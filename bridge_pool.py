"""Independent admission, thread budgets and visible slots for each backend."""
from contextlib import asynccontextmanager
import anyio

from bridge_config import PROVIDERS


class BackendPool:
    def __init__(self, key, backend, capacity):
        self.key, self.backend, self.capacity = key, backend, capacity
        self.gate = anyio.Semaphore(capacity)
        # Do not silently inherit AnyIO's default shared 40-worker ceiling.
        self.workers = anyio.CapacityLimiter(capacity)
        self.occupied = set()

    @asynccontextmanager
    async def admit(self):
        async with self.gate:
            slot = 0
            while slot in self.occupied:
                slot += 1
            self.occupied.add(slot)
            try:
                yield
            finally:
                self.occupied.remove(slot)

    def snapshot(self):
        backend = self.backend
        return {
            'provider': self.key, 'name': PROVIDERS[self.key]['name'],
            'model': PROVIDERS[self.key]['model'], 'backend': getattr(backend, 'backend_id', 'wechat-4.1.13.65'),
            'max_concurrency': self.capacity, 'active_requests': len(self.occupied),
            'occupied_slots': sorted(self.occupied),
            'queued_requests': self.gate.statistics().tasks_waiting,
            'status': 'ok' if backend.healthy else 'degraded',
            'last_error': backend.last_error, 'last_success': backend.last_success,
            'agent_connected': backend.agent_connected, 'last_pid': backend.last_pid,
            'completion': getattr(backend, 'last_completion', None),
        }
