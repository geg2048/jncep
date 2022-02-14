from functools import partial, wraps
import logging
from typing import List

import attr
import outcome
import trio

logger = logging.getLogger(__name__)


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return trio.run(partial(f, *args, **kwargs))

    return wrapper


# taken from trio-future (but needs py > 3.7.0) so done here
@attr.s
class Future:
    result_chan: trio.abc.ReceiveChannel = attr.ib()

    async def get(self):
        future_outcome = await self.outcome()
        return future_outcome.unwrap()

    async def outcome(self) -> outcome.Outcome:
        try:
            async with self.result_chan:
                return await self.result_chan.receive()
        except trio.ClosedResourceError:
            raise RuntimeError(
                "Trio resource closed (did you try to call outcome twice on this "
                "future?"
            )


def background(nursery: trio.Nursery, async_fn) -> Future:
    send_chan, recv_chan = trio.open_memory_channel(1)

    async def producer():
        return_val = await outcome.acapture(async_fn)
        # Shield sending the result from parent cancellation.
        with trio.CancelScope(shield=True):
            async with send_chan:
                await send_chan.send(return_val)

    nursery.start_soon(producer)
    return Future(recv_chan)


def gather(nursery: trio.Nursery, futures: List[Future]) -> Future:
    result_list = [None] * len(futures)
    parent_send_chan, parent_recv_chan = trio.open_memory_channel(0)
    child_send_chan, child_recv_chan = trio.open_memory_channel(0)

    async def producer():
        async with child_send_chan:
            for i in range(len(futures)):
                nursery.start_soon(child_producer, i, child_send_chan.clone())

    async def child_producer(i: int, out_chan):
        async with futures[i].result_chan:
            return_val = await futures[i].result_chan.receive()
            result_list[i] = return_val
            async with out_chan:
                await out_chan.send(i)

    async def receiver():
        async with child_recv_chan:
            async for i in child_recv_chan:
                # Just consume all results from the channel until exhausted
                pass
        # And then wrap up the result and push it to the parent channel
        errors = [e.error for e in result_list if isinstance(e, outcome.Error)]
        if len(errors) > 0:
            result = outcome.Error(trio.MultiError(errors))
        else:
            result = outcome.Value([o.unwrap() for o in result_list])
        async with parent_send_chan:
            await parent_send_chan.send(result)

    # Start parent producer, which will in turn start all children
    # (doing this inside the nursery because it needs to act async)
    nursery.start_soon(producer)
    nursery.start_soon(receiver)
    return Future(parent_recv_chan)
