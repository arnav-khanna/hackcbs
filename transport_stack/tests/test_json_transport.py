"""Protocol, optional-feature, reliability, API and modem regression tests."""
import asyncio
import base64
from dataclasses import replace
import os
import struct
import time
import numpy as np
import pytest
import yaml

from json_transport.config import Config
from json_transport.codec import Codec, PREFIX
from json_transport.engine import Transport
from json_transport.links import MemoryLink
from json_transport.security import Security
from json_transport.wire import Frame, DATA, ACK
from pluto_json_radio.keys import generate_ed25519_keypair, generate_x25519_keypair


def identities(tmp_path):
    keys = {node: (generate_x25519_keypair(), generate_ed25519_keypair()) for node in ('host','server')}
    paths = {}
    for node, peer in [('host','server'),('server','host')]:
        document = {'link': {'default_receiver_address': peer}, 'security': {
            'private_key': keys[node][0]['private_key'], 'peer_public_keys': {peer: keys[peer][0]['public_key']},
            'signing_private_key': keys[node][1]['private_key'],
            'peer_signing_public_keys': {peer: keys[peer][1]['public_key']}}}
        path = tmp_path / f'{node}.yaml'
        path.write_text(yaml.safe_dump(document))
        paths[node] = str(path)
    return paths


def configs(options=None, paths=None):
    settings = options or {}
    if paths:
        settings = dict(settings, modes={node: {'identity_file': path} for node,path in paths.items()})
    return Config(settings,'host'), Config(settings,'server')


async def pair_exchange(first_config, second_config, obj=None, configure=None):
    first_link, second_link = MemoryLink.pair()
    if configure:
        configure(first_link,second_link)
    first,second = Transport(first_config,first_link),Transport(second_config,second_link)
    await first.start(); await second.start()
    try:
        payload = obj if obj is not None else {'text':'hello π 世界', 'values':[1,2.5,None,True], 'nested':{'n':9}}
        result = await first.send(payload)
        assert (await asyncio.wait_for(second.inbox.get(),2))['json'] == payload
        reply = await second.send({'reply':payload})
        assert (await asyncio.wait_for(first.inbox.get(),2))['json'] == {'reply':payload}
        assert result['status'] == reply['status'] == ('delivered' if first_config.reliability['enabled'] else 'submitted')
        return result, first.health(), second.health()
    finally:
        await first.close(); await second.close()


@pytest.mark.parametrize('compression',['none','brotli','zstd','lz4'])
@pytest.mark.parametrize('encryption',['none','aes256'])
@pytest.mark.parametrize('key_exchange',['disabled','x25519'])
def test_independent_options(compression,encryption,key_exchange,tmp_path,monkeypatch):
    monkeypatch.setenv('PLUTO_AES_KEY',base64.b64encode(os.urandom(32)).decode())
    options={'compression':{'algorithm':compression,'min_bytes':0},
             'encryption':{'algorithm':encryption},'key_exchange':{'algorithm':key_exchange},
             'limits':{'packet_bytes':128}}
    a,b = configs(options,identities(tmp_path))
    asyncio.run(pair_exchange(a,b,{'data':['repeated content'*40]*20}))


@pytest.mark.parametrize('session',[False,True])
@pytest.mark.parametrize('signing',['none','ed25519'])
@pytest.mark.parametrize('distribution',[False,True])
def test_sessions_signatures_distribution(session,signing,distribution,tmp_path):
    options={'encryption':{'algorithm':'aes256'},'key_exchange':{'algorithm':'x25519'},
             'signing':{'algorithm':signing},'session':{'enabled':session},
             'key_distribution':{'enabled':distribution}}
    if distribution and not session:
        with pytest.raises(ValueError): configs(options)
        return
    a,b = configs(options,identities(tmp_path))
    asyncio.run(pair_exchange(a,b))


def test_plain_session_without_encryption_or_kex():
    a,b = configs({'session':{'enabled':True}})
    asyncio.run(pair_exchange(a,b))


def test_psk_session(tmp_path,monkeypatch):
    monkeypatch.setenv('PLUTO_AES_KEY',base64.b64encode(os.urandom(32)).decode())
    a,b = configs({'session':{'enabled':True},'encryption':{'algorithm':'aes256'}})
    asyncio.run(pair_exchange(a,b))


def test_loss_duplicates_and_selective_retransmission():
    dropped=set()
    def fault(first, second):
        def transform(raw):
            frame=Frame.decode(raw)
            if frame.kind == DATA and frame.index in (0,3) and frame.message_id not in dropped:
                dropped.add(frame.message_id)
                return []
            return [raw, raw]
        first.filter=transform
        second.filter=lambda raw:[raw,raw]
    a,b=configs({'limits':{'packet_bytes':128},'reliability':{'ack_timeout_ms':3}})
    result,_,other=asyncio.run(pair_exchange(a,b,{'data':'x'*6000},fault))
    assert result['retransmitted_fragments'] == 1
    assert other['stats']['messages_received'] == 1


def test_lost_final_ack_delivers_once():
    def fault(first,second):
        dropped=set()
        def transform(raw):
            frame=Frame.decode(raw)
            if frame.kind == ACK and frame.message_id not in dropped:
                dropped.add(frame.message_id); return []
            return [raw]
        second.filter=transform
    a,b=configs({'reliability':{'ack_timeout_ms':3}})
    result,_,other=asyncio.run(pair_exchange(a,b,configure=fault))
    assert result['retransmitted_fragments'] == 1
    assert other['stats']['messages_received']==1


def test_reordered_fragments():
    def fault(first,second):
        held=[]
        def transform(raw):
            frame=Frame.decode(raw)
            if frame.kind != DATA:
                return [raw]
            held.append(raw)
            if frame.request_ack:
                batch=list(reversed(held)); held.clear(); return batch
            return []
        first.filter=transform
    a,b=configs({'limits':{'packet_bytes':128},'reliability':{'ack_timeout_ms':3}})
    asyncio.run(pair_exchange(a,b,{'data':'z'*15000},fault))


def test_unreliable_mode():
    a,b=configs({'reliability':{'enabled':False}})
    asyncio.run(pair_exchange(a,b))


def test_retry_exhaustion_and_bounded_cleanup():
    async def scenario():
        a,b=configs({'limits':{'reassembly_timeout_s':0.01},'reliability':{'ack_timeout_ms':2,'max_retries':1}})
        one,two=MemoryLink.pair(); two.filter=lambda raw:[]
        sender,receiver=Transport(a,one),Transport(b,two)
        await sender.start(); await receiver.start()
        try:
            with pytest.raises(TimeoutError): await sender.send({'hello':1})
            assert receiver.inbox.qsize()==1
            assert sender.stats['send_failures']==1
            await receiver._accept_data(Frame(DATA,1,2,1234,0,2,b'x'))
            await asyncio.sleep(0.03)
            assert not receiver.assemblies and receiver.buffer_bytes==0
        finally: await sender.close(); await receiver.close()
    asyncio.run(scenario())


def test_receive_queue_backpressure():
    async def scenario():
        a,b=configs({'limits':{'receive_queue':1},'reliability':{'ack_timeout_ms':2,'max_retries':1}})
        one,two=MemoryLink.pair(); sender,receiver=Transport(a,one),Transport(b,two)
        await sender.start(); await receiver.start()
        try:
            await sender.send({'first':1})
            with pytest.raises(TimeoutError): await sender.send({'second':2})
            assert receiver.inbox.qsize()==1
            assert receiver.stats['messages_received']==1
        finally: await sender.close(); await receiver.close()
    asyncio.run(scenario())


def test_tamper_downgrade_and_wrong_key(tmp_path,monkeypatch):
    monkeypatch.setenv('PLUTO_AES_KEY',base64.b64encode(os.urandom(32)).decode())
    a,b=configs({'encryption':{'algorithm':'aes256'},'signing':{'algorithm':'ed25519'}},identities(tmp_path))
    ca,cb=Codec(a,Security(a)),Codec(b,Security(b))
    blob,_=ca.encode({'secret':1},42)
    with pytest.raises(Exception): cb.decode(blob[:-1]+bytes([blob[-1]^1]),42,1,2)
    with pytest.raises(Exception): cb.decode(blob,43,1,2)
    plain=Codec(Config(),Security(Config()))
    unencrypted,_=plain.encode({'secret':1},42)
    with pytest.raises(ValueError,match='Encryption policy'): cb.decode(unencrypted,42,1,2)
    monkeypatch.setenv('PLUTO_AES_KEY',base64.b64encode(os.urandom(32)).decode())
    wrong=Codec(b,Security(b))
    with pytest.raises(Exception): wrong.decode(blob,42,1,2)


@pytest.mark.parametrize('algorithm',['brotli','zstd','lz4'])
def test_decompression_limit(algorithm):
    config=Config({'compression':{'algorithm':algorithm,'min_bytes':0}})
    codec=Codec(config,Security(config))
    code,compressed=codec.compress(b'x'*1000000)
    with pytest.raises(ValueError): codec.decompress(code,compressed,10)


def test_wire_crc_and_validation(monkeypatch):
    monkeypatch.delenv("PLUTO_AES_KEY", raising=False)
    frame=Frame(DATA,1,2,7,0,1,b'test',True)
    assert Frame.decode(frame.encode())==frame
    raw=frame.encode()
    with pytest.raises(ValueError): Frame.decode(raw[:-1]+bytes([raw[-1]^1]))
    with pytest.raises(ValueError): Config({'compression':{'algorithm':'unknown'}})
    with pytest.raises(ValueError): Config({'reliability':{'window_size':33}})
    with pytest.raises(ValueError): Security(Config({'encryption':{'algorithm':'aes256'}}))
    with pytest.raises(ValueError): Config({'unknown':1})


def test_api_send_receive_and_validation():
    import httpx
    from json_transport.api import create_app
    async def scenario():
        a,b=configs(); one,two=MemoryLink.pair()
        app_a,app_b=create_app(a,one),create_app(b,two)
        async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a),base_url='http://host') as host, httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b),base_url='http://server') as server:
                response=await host.post('/send',json={'payload':{'n':42}})
                assert response.status_code==200 and response.json()['status']=='delivered'
                response=await server.get('/receive')
                assert response.json()['json']=={'payload':{'n':42}}
                assert (await server.get('/receive')).status_code==204
                assert (await server.get('/receive?timeout_ms=1')).status_code==204
                assert (await host.post('/send',json=[])).status_code==400
                assert (await host.post('/send',content=b'{bad')).status_code==400
                assert (await host.get('/health')).json()['running']
                assert (await host.post('/session')).json()=={'enabled':False,'established':False}
    asyncio.run(scenario())


def test_modem_recovers_binary_fragments_across_dma_boundaries():
    from json_transport.radio import RadioLink
    link=RadioLink(Config())
    packets=[Frame(DATA,1,2,123,i,3,os.urandom(200)).encode() for i in range(3)]
    iq=np.concatenate([np.zeros(3,dtype=np.complex64)]+[np.concatenate((link.modulate(p),np.zeros(400,dtype=np.complex64))) for p in packets])
    recovered=[]
    for index in range(0,len(iq),4096):
        recovered.extend(link.decode_iq(iq[index:index+4096]))
    assert set(recovered)==set(packets)
    assert len(recovered)==len(packets)


def test_modem_dedupes_timing_offsets_but_preserves_real_repeated_bursts():
    from json_transport.radio import RadioLink
    link=RadioLink(Config())
    raw=Frame(DATA,1,2,42,payload=os.urandom(200)).encode()
    wave=link.modulate(raw)
    capture=np.concatenate((wave,np.zeros(4000,dtype=np.complex64),wave,np.zeros(4000,dtype=np.complex64)))
    recovered=[]
    for start in range(0,len(capture),4096):
        recovered.extend(link.decode_iq(capture[start:start+4096]))
    assert recovered==[raw,raw]
    for _ in range(20):link.decode_iq(np.zeros(4096,dtype=np.complex64))
    assert len(link.overlap) < 120*link.sps


def test_simultaneous_bidirectional_sends():
    async def scenario():
        a,b=configs();one,two=MemoryLink.pair();first,second=Transport(a,one),Transport(b,two)
        await first.start();await second.start()
        try:
            results=await asyncio.gather(first.send({'from':'host','data':'a'*10000}),second.send({'from':'server','data':'b'*10000}))
            assert all(r['status']=='delivered' for r in results)
            assert (await first.inbox.get())['json']['from']=='server'
            assert (await second.inbox.get())['json']['from']=='host'
        finally:await first.close();await second.close()
    asyncio.run(scenario())


def test_udp_loss_delay_duplicates_and_reordering():
    from json_transport.links import SimulationLink
    import socket
    def free_port():
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
    port_a,port_b=free_port(),free_port()
    while port_b==port_a:port_b=free_port()
    options={'simulation':{'loss_rate':0.15,'duplicate_rate':0.2,'delay_ms':1,'jitter_ms':2},
             'reliability':{'ack_timeout_ms':8,'max_retries':15},
             'modes':{'host':{'simulation_port':port_a,'peer_simulation_port':port_b},
                      'server':{'simulation_port':port_b,'peer_simulation_port':port_a}}}
    async def scenario():
        a,b=configs(options);first,second=Transport(a,SimulationLink(a)),Transport(b,SimulationLink(b))
        await first.start();await second.start()
        try:
            obj={'text':'fault simulation'*1000}
            result=await first.send(obj)
            assert result['status']=='delivered'
            assert (await second.inbox.get())['json']==obj
            assert second.inbox.empty()
            assert first.stats['retransmissions']>0
        finally:await first.close();await second.close()
    asyncio.run(scenario())


def test_authenticated_ack_rejects_tampering(monkeypatch):
    from json_transport.engine import Outbound, ACK_BODY
    monkeypatch.setenv('PLUTO_AES_KEY',base64.b64encode(os.urandom(32)).decode())
    async def scenario():
        a,b=configs({'encryption':{'algorithm':'aes256'}})
        one,two=MemoryLink.pair();sender,receiver=Transport(a,one),Transport(b,two)
        sender.pending[123]=Outbound(1)
        body=ACK_BODY.pack(1,1)
        frame=Frame(ACK,2,1,123,0,1,body)
        tag=receiver.security.authenticate(frame.header()+body)
        bad=replace(frame,payload=body+tag[:-1]+bytes([tag[-1]^1]))
        with pytest.raises(ValueError,match='Authentication'):sender._accept_ack(bad)
        assert not sender.pending[123].complete
        sender._accept_ack(replace(frame,payload=body+tag))
        assert sender.pending[123].complete
    asyncio.run(scenario())


def test_active_reassembly_refreshes_inactivity_timeout():
    async def scenario():
        a,b=configs({'limits':{'reassembly_timeout_s':0.02}})
        first,second=MemoryLink.pair();receiver=Transport(b,second)
        await receiver.start()
        try:
            blob,_=Codec(a,Security(a)).encode({'text':'a large active message'},123)
            width=10;count=(len(blob)+width-1)//width
            for index in range(count):
                await receiver._accept_data(Frame(DATA,1,2,123,index,count,blob[index*width:(index+1)*width]))
                await asyncio.sleep(0.012)
            assert (await receiver.inbox.get())['json']=={'text':'a large active message'}
            assert receiver.stats['expired_messages']==0
        finally:await receiver.close()
    asyncio.run(scenario())


def test_console_and_continuous_receive_while_sending():
    import httpx
    from json_transport.api import create_app
    async def scenario():
        a,b=configs();one,two=MemoryLink.pair();app_a,app_b=create_app(a,one),create_app(b,two)
        async with app_a.router.lifespan_context(app_a),app_b.router.lifespan_context(app_b):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a),base_url='http://host') as host,httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b),base_url='http://server') as server:
                page=await host.get('/')
                assert page.status_code==200 and 'text/html' in page.headers['content-type']
                assert 'Live received JSON' in page.text and 'JSON.parse' in page.text
                for index in range(3):
                    listening=asyncio.create_task(server.get('/receive?timeout_ms=5000'))
                    await asyncio.sleep(0)
                    obj={'edited':{'seq':index},'values':[1,True,None]}
                    assert (await host.post('/send',json=obj)).json()['status']=='delivered'
                    assert (await listening).json()['json']==obj
                # Cancelled consumers must leave the next message available.
                listening=asyncio.create_task(server.get('/receive?timeout_ms=5000'))
                await asyncio.sleep(0.01)
                listening.cancel();await asyncio.gather(listening,return_exceptions=True)
                assert (await host.post('/send',json={'after_close':True})).status_code==200
                assert (await server.get('/receive')).json()['json']=={'after_close':True}
    asyncio.run(scenario())


def test_disconnected_receive_releases_inbox_waiter():
    from json_transport.api import create_app
    class DisconnectedRequest:
        async def is_disconnected(self):return True
    async def scenario():
        a,_=configs();one,_=MemoryLink.pair();app=create_app(a,one)
        async with app.router.lifespan_context(app):
            route=next(route for route in app.routes if route.path=='/receive')
            response=await route.endpoint(DisconnectedRequest(),timeout_ms=5000)
            assert response.status_code==499
            app.state.transport.inbox.put_nowait({'still':'available'})
            assert app.state.transport.inbox.get_nowait()=={'still':'available'}
    asyncio.run(scenario())
