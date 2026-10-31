"""Async datagram interface and deterministic UDP simulation."""
from __future__ import annotations
import asyncio
import random


class SimulationLink(asyncio.DatagramProtocol):
    def __init__(self, config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=4096)
        self.random = random.Random(config.simulation['seed'] + config.local['node_id'])
        self.transport = None
        self.tasks = set()
        self.dropped = 0

    async def start(self):
        option = self.config.simulation
        self.peer = (option['host'], self.config.local['peer_simulation_port'])
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self, local_addr=(option['host'], self.config.local['simulation_port']))

    def datagram_received(self, data, addr):
        # Simulation accepts only its configured peer, including source port.
        if addr[1] != self.peer[1]:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1

    def error_received(self, exc):
        pass  # A peer may not have started yet; reliability handles the timeout.

    async def _delayed(self, packet, delay):
        await asyncio.sleep(delay)
        if self.transport:
            self.transport.sendto(packet, self.peer)

    async def send_many(self, packets):
        option = self.config.simulation
        for packet in packets:
            if self.random.random() < option['loss_rate']:
                self.dropped += 1
                continue
            for _ in range(2 if self.random.random() < option['duplicate_rate'] else 1):
                delay = max(0, option['delay_ms'] + self.random.uniform(-option['jitter_ms'], option['jitter_ms'])) / 1000
                if delay:
                    task = asyncio.create_task(self._delayed(packet, delay))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:
                    self.transport.sendto(packet, self.peer)

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.transport:
            self.transport.close()
            self.transport = None

    def airtime(self, packets):
        return 0.0


class MemoryLink:
    """Datagram simulation for tests and CPU/transport benchmarks, with no hardware."""
    def __init__(self):
        self.queue = asyncio.Queue()
        self.peer = None
        self.filter = lambda packet: [packet]

    @classmethod
    def pair(cls):
        first, second = cls(), cls()
        first.peer, second.peer = second, first
        return first, second

    async def start(self):
        pass

    async def send_many(self, packets):
        for packet in packets:
            for output in self.filter(packet):
                self.peer.queue.put_nowait(output)
        await asyncio.sleep(0)

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        pass

    def airtime(self, packets):
        return 0.0
