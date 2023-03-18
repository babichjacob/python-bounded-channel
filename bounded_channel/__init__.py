"""
A queue for sending values between asynchronous tasks.

This module provides a bounded channel.
It has a limit on the number of messages that the channel can store,
and if this limit is reached, trying to send another message will wait
until a message is received from the channel.

The channel constructor function provides separate send and receive handles, `Sender` and `Receiver`
If there is no message to read, the current task will be notified when a new value is sent.
`Sender` allows sending values into the channel.
If the channel is at capacity, the send is rejected
and the task will be notified when additional capacity is available.
In other words, the channel provides backpressure.

This channel is also suitable for the single-producer single-consumer use-case.
(Unless you only need to send one message, in which case you should use
the [`oneshot` channel](https://pypi.org/project/babichjacob-oneshot-channel/).)

# Disconnection
When all `Sender` handles have been dropped,
it is no longer possible to send values into the channel.
This is considered the termination event of the stream.
As such, `Receiver.recv` returns `NONE()`.

If the `Receiver` handle is dropped, then messages can no longer be read out of the channel.
In this case, all further attempts to send will result in an error.

# Examples

## Using a bounded channel to incrementally stream the results of a series of computations

>>> async def some_computation(input: int) -> str:
...     return f"the result of computation {input}"

>>> async def producer(tx: Sender[str]):
...     for i in range(10):
...         res = await some_computation(i);
...         (await tx.send(res)).unwrap()

>>> async def main():
...     (tx, rx) = channel(100);
...
...     create_task(producer(tx))
...     del tx # Remove this extra reference to the Sender to ensure proper RAII behavior
...
...     async for res in rx:
...         print("got =", res)

>>> from asyncio import run
>>> run(main())
got = the result of computation 0
got = the result of computation 1
got = the result of computation 2
got = the result of computation 3
got = the result of computation 4
got = the result of computation 5
got = the result of computation 6
got = the result of computation 7
got = the result of computation 8
got = the result of computation 9


The argument to `bounded_channel` is the channel capacity.
This is the maximum number of values that can be stored in the channel
pending receipt at any given time.
Properly setting this value is key in implementing robust programs
as the channel capacity plays a critical part in handling back pressure.

A common concurrency pattern for resource management is to spawn a task
dedicated to managing that resource and using message passing
between other tasks to interact with the resource.
The resource may be anything that may not be concurrently used.
Some examples include a socket and program state.
For example, if multiple tasks need to send data over a single socket,
spawn a task to manage the socket and use a channel to synchronize.


## Using a task to synchronize an integer counter

The mpsc and oneshot channels can be combined to provide a request / response type
synchronization pattern with a shared resource.
A task is spawned to synchronize a resource and waits on commands received on `bounded_channel`.
Each command includes a [`oneshot_channel`](https://pypi.org/project/babichjacob-oneshot-channel/)
`Sender` on which the result of the command is sent.


Each task sends an “fetch and increment” command.
The counter value **before** the increment is sent over the provided `oneshot` channel.

>>> from asyncio import gather

>>> class Increment(): pass
>>> # Other commands can be added here
>>> Command = Increment

>>> import oneshot_channel

>>> async def manage_counter(cmd_rx: Receiver[tuple[Command, oneshot_channel.Sender[int]]]):
...     counter = 0
...     async for (cmd, response) in cmd_rx:
...         match cmd:
...             case Increment():
...                 prev = counter
...                 counter += 1
...                 response.send(prev).unwrap();

>>> async def send_increment_commands(cmd_tx: Sender[tuple[Command, oneshot_channel.Sender[int]]]):
...     resp_tx, resp_rx = oneshot_channel.channel();
...
...     (await cmd_tx.send((Increment(), resp_tx))).ok().unwrap()
...     res = (await resp_rx).unwrap()
...
...     print("previous value =", res)

>>> async def main():
...     cmd_tx, cmd_rx = channel(100)
...
...     # Spawn a task to manage the counter
...     create_task(manage_counter(cmd_rx))
...     del cmd_rx # Remove this extra reference to the Receiver to ensure proper RAII behavior
...
...     tasks = []
...
...     # Spawn tasks that will send the increment command
...     for _ in range(10):
...         tasks.append(send_increment_commands(cmd_tx))
...     del cmd_tx # Remove this extra reference to the Sender to ensure proper RAII behavior
...
...     # Wait for all tasks to complete
...     await gather(*tasks)

>>> from asyncio import run
>>> run(main())
previous value = 0
previous value = 1
previous value = 2
previous value = 3
previous value = 4
previous value = 5
previous value = 6
previous value = 7
previous value = 8
previous value = 9
"""


from asyncio import FIRST_COMPLETED, CancelledError, Event, Queue, create_task, wait
from dataclasses import dataclass
from typing import AsyncIterator, Generic, TypeVar

from option_and_result import (
    NONE,
    Err,
    Ok,
    Option,
    Result,
    Some,
)

T = TypeVar("T")


@dataclass
class TrySendErrorFull(Generic[T]):
    """
    The data could not be sent on the channel because the channel is currently full
    and sending would require blocking.
    """

    value: T

    def __str__(self):
        return "no available capacity"


@dataclass
class TrySendErrorClosed(Generic[T]):
    "The receive half of the channel was explicitly closed or has been dropped."

    value: T

    def __str__(self):
        return "channel closed"


@dataclass
class SendError(Generic[T]):
    "Error returned by the `Sender`"

    value: T

    def __str__(self):
        return "channel closed"


@dataclass
class Sender(Generic[T]):
    "Sends values to the associated `Receiver`"

    _queue: Queue[T]
    _closed: Event
    _disconnected: Event

    async def send(self, value: T) -> Result[None, SendError[T]]:
        """
        Sends a value, waiting until there is capacity.

        A successful send occurs when it is determined that the other end of the channel
        has not hung up already.
        An unsuccessful send would be one where the corresponding receiver has already been closed.
        Note that a return value of `Err` means that the data will never be received,
        but a return value of `Ok` does not mean that the data will be received.
        It is possible for the corresponding receiver to hang up
        immediately after this function returns `Ok`.

        # Errors

        If the receive half of the channel is closed, either due to `close` being called
        or the `Receiver` handle dropping, the function returns an error.
        The error includes the value passed to send.
        """

        closed_event = create_task(self._closed.wait())
        put_task = create_task(self._queue.put(value))
        try:
            done, _pending = await wait(
                [put_task, closed_event], return_when=FIRST_COMPLETED
            )
        except CancelledError:
            closed_event.cancel()
            put_task.cancel()
            raise

        if closed_event in done:
            put_task.cancel()
            if self._queue.empty():
                self._disconnected.set()
            return Err(SendError(value))

        assert put_task in done
        closed_event.cancel()
        return Ok(None)

    async def closed(self):
        """
        Completes when the receiver has dropped.

        This allows the producers to get notified when interest in the produced values is canceled
        and immediately stop doing work.
        """
        await self._closed.wait()

    def try_send(
        self, value: T
    ) -> Result[None, TrySendErrorFull[T] | TrySendErrorClosed[T]]:
        """
        Attempts to immediately send a message on this `Sender`

        This method differs from `send` by returning immediately if the channel's buffer is full
        or no receiver is waiting to acquire some data.
        Compared with `send`, this function has two failure cases
        instead of one (one for disconnection, one for a full buffer).
        """

        if self._queue.full():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TrySendErrorFull(value))  # type: ignore

        if self._closed.is_set():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TrySendErrorClosed(value))  # type: ignore

        # This cannot raise an exception given the guards above
        self._queue.put_nowait(value)

        return Ok(None)

    def is_closed(self):
        """
        Checks if the channel has been closed.
        This happens when the `Receiver` is dropped, or when the `Receiver.close` method is called.
        """
        return self._closed.is_set()

    def max_capacity(self):
        """
        Returns the maximum buffer capacity of the channel.

        The maximum capacity is the buffer capacity initially specified when calling `channel`.
        This is distinct from capacity, which returns the *current* available buffer capacity:
        as messages are sent and received, the value returned by `capacity` will go up or down,
        whereas the value returned by `max_capacity` will remain constant.
        """

        return self._queue.maxsize

    def capacity(self):
        """
        Returns the current capacity of the channel.

        The capacity goes down when sending a value by calling `send`.
        The capacity goes up when values are received by the `Receiver`.
        This is distinct from `max_capacity`, which always returns buffer capacity
        initially specified when calling `channel`
        """

        return self._queue.maxsize - self._queue.qsize()

    def __del__(self):
        self._closed.set()
        if self._queue.empty():
            self._disconnected.set()


@dataclass
class TryRecvErrorEmpty:
    """
    This channel is currently empty, but the `Sender`(s) have not yet
    disconnected, so data may yet become available.
    """

    def __str__(self):
        return "receiving on an empty channel"


@dataclass
class TryRecvErrorDisconnected:
    """
    The channel's sending half has become disconnected,
    and there will never be any more data received on it.
    """

    def __str__(self):
        return "receiving on a closed channel"


@dataclass
class Receiver(Generic[T]):
    "Receives values from the associated `Sender`"

    _queue: Queue[T]
    _closed: Event
    _disconnected: Event

    async def recv(self) -> Option[T]:
        """
        Receives the next value for this receiver.

        This method returns `NONE()` if the channel has been closed
        and there are no remaining messages in the channel's buffer.
        This indicates that no further values can ever be received from this `Receiver`.
        The channel is closed when all senders have been dropped, or when `close` is called.

        If there are no messages in the channel's buffer, but the channel has not yet been closed,
        this method will sleep until a message is sent or the channel is closed.
        """

        disconnected_event = create_task(self._disconnected.wait())
        get_task = create_task(self._queue.get())
        try:
            done, _pending = await wait(
                [get_task, disconnected_event], return_when=FIRST_COMPLETED
            )
        except CancelledError:
            disconnected_event.cancel()
            get_task.cancel()
            raise

        if disconnected_event in done:
            get_task.cancel()
            return NONE()

        assert get_task in done
        disconnected_event.cancel()
        if self._closed.is_set() and self._queue.empty():
            self._disconnected.set()
        return Some(get_task.result())

    def try_recv(self) -> Result[T, TryRecvErrorEmpty | TryRecvErrorDisconnected]:
        """
        Tries to receive the next value for this receiver.

        This method returns the `Empty` error if the channel is currently empty,
        but there are still outstanding senders.

        This method returns the `Disconnected` error if the channel is currently empty,
        and there are no outstanding senders.
        """

        if self._disconnected.is_set():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TryRecvErrorDisconnected())  # type: ignore

        if self._queue.empty():
            # Not actually a type error but Pylance (in VS Code) thinks it is
            return Err(TryRecvErrorEmpty())  # type: ignore

        # This cannot raise an exception given the guards above
        return Ok(self._queue.get_nowait())

    async def __aiter__(self) -> AsyncIterator[T]:
        while True:
            received = await self.recv()

            if received.is_none():
                return

            yield received.unwrap()

    def close(self):
        """
        Closes the receiving half of a channel without dropping it.

        This prevents any further messages from being sent on the channel
        while still enabling the receiver to drain messages that are buffered.

        To guarantee that no messages are dropped, after calling `close()`,
        `recv()` must be called until `NONE()` is returned.
        """

        self._closed.set()
        if self._queue.empty():
            self._disconnected.set()

    def __del__(self):
        self.close()


def channel(buffer: int) -> tuple[Sender[T], Receiver[T]]:
    """
    Creates a bounded channel for communicating between asynchronous tasks with backpressure.

    The channel will buffer up to the provided number of messages.
    Once the buffer is full, attempts to send new messages will wait
    until a message is received from the channel.
    The provided buffer capacity must be at least 1.

    All data sent on `Sender` will become available on `Receiver` in the same order as it was sent.

    References to the `Sender` can be implicitly copied to `send` to the same channel
    from multiple code locations. Likewise for the `Receiver` to receive from multiple locations.

    If the `Receiver` is disconnected while trying to `send`,
    the `send` method will return a `SendError`.

    Similarly, if `Sender` is disconnected while trying to `recv`,
    the `recv` method will return `NONE()`.
    """

    assert buffer > 0, "bounded channels need a positive integer buffer size"

    queue: Queue[T] = Queue(buffer)

    closed = Event()
    disconnected = Event()

    sender = Sender(queue, closed, disconnected)
    receiver = Receiver(queue, closed, disconnected)

    return (sender, receiver)
