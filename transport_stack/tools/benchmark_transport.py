#!/usr/bin/env python3
"""Measure confirmed transport latency and large-message goodput (no HTTP)."""
import argparse
import asyncio
import json
from pathlib import Path
import random
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from json_transport.config import Config
from json_transport.engine import Transport
from json_transport.links import MemoryLink, SimulationLink


def summary(values):
    values=sorted(values)
    return {'count':len(values),'p50_ms':round(statistics.median(values),3),
            'p95_ms':round(values[min(len(values)-1,int(len(values)*0.95))],3),
            'max_ms':round(max(values),3)}


async def run(args):
    first_config=Config.load(args.config,'host')
    second_config=Config.load(args.peer_config or args.config,'server')
    if args.backend=='memory':
        first_link,second_link=MemoryLink.pair()
    elif args.backend=='udp':
        first_link,second_link=SimulationLink(first_config),SimulationLink(second_config)
    else:
        from json_transport.radio import RadioLink
        first_link,second_link=RadioLink(first_config),RadioLink(second_config)
    first,second=Transport(first_config,first_link),Transport(second_config,second_link)
    await first.start()
    try:
        await second.start()
        try:
            latencies=[]; retransmissions=0
            for index in range(args.messages):
                sender,receiver=(first,second) if index%2==0 else (second,first)
                # Optional sessions always start from the host.
                if index==0 and first_config.session['enabled']:
                    await first.establish()
                obj={'text':'hello','seq':index}
                result=await sender.send(obj)
                incoming=await asyncio.wait_for(receiver.inbox.get(),2)
                assert incoming['json']==obj
                latencies.append(result['transport_latency_ms'])
                retransmissions+=result['retransmitted_fragments']
            rng=random.Random(42)
            text=''.join(rng.choices('abcdefghijklmnopqrstuvwxyz0123456789',k=args.large_bytes))
            started=time.perf_counter()
            result=await first.send({'data':text})
            incoming=await asyncio.wait_for(second.inbox.get(),5)
            assert incoming['json']=={'data':text}
            seconds=time.perf_counter()-started
            report={'backend':args.backend,'compression':first_config.compression['algorithm'],
                    'encryption':first_config.encryption['algorithm'],'small_message_latency':summary(latencies),
                    'small_message_retransmitted_fragments':retransmissions,
                    'large_message':dict(result,goodput_kib_s=round(result['json_bytes']/1024/seconds,3)),
                    'notes':'ACK-confirmed transport latency; excludes HTTP, includes codec and radio/UDP. Memory/UDP are not RF measurements.'}
            print(json.dumps(report,indent=2))
            if args.output:
                Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
        finally:
            await second.close()
    finally:
        await first.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['memory','udp','radio'],default='memory')
    parser.add_argument('--config',default='configs/transport.yaml')
    parser.add_argument('--peer-config')
    parser.add_argument('--messages',type=int,default=30)
    parser.add_argument('--large-bytes',type=int,default=32768)
    parser.add_argument('--output')
    args=parser.parse_args()
    if args.messages<1 or args.large_bytes<1: parser.error('sizes and counts must be positive')
    asyncio.run(run(args))
