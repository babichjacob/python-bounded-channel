<h1 align="center">🪢 Bounded Channel</h1>

This library uses documentation copied and pasted from [Tokio's `sync::mpsc` library](https://docs.rs/tokio/latest/tokio/sync/mpsc/index.html), which they have generously published under the MIT license. 🙏

This is a Python implementation of [their bounded channel](https://docs.rs/tokio/latest/tokio/sync/mpsc/fn.channel.html).

## 💻 Installation

This package is [published to PyPI as `babichjacob-bounded-channel`](https://pypi.org/project/babichjacob-bounded-channel/).

## 🛠 Usage

```py
from asyncio import create_task, gather, run, sleep
from itertools import count

from bounded_channel import bounded_channel, Receiver, Sender


async def producer(sender: Sender[int]):
    "Produces integer values as long as there is a receiver to receive them"
    for value in count():
        await sleep(0.02)

        res = await sender.send(value)

        # No receivers are listening anymore
        if res.is_err():
            break


async def consumer(receiver: Receiver[int]):
    async for value in receiver:
        await sleep(0.03)

        print("received", value)

        if value >= 100:
            # Signal to please stop producing values
            receiver.close()
            # From then on, the remaining buffered values will be received
            # until they run out for good (to a maximum of 165 or so)
            # (it's dependent on the difference of speed between the producer and consumer)

    # Alternatively, the loop could be broken out of
    # and any extra buffered values would be ignored


async def main():
    sender, receiver = channel(64)

    producer_task = create_task(producer(sender))
    consumer_task = create_task(consumer(receiver))

    # Drop extra references to the sender and receiver
    del sender
    del receiver
    # so their RAII semantics behave properly

    await gather(producer_task, consumer_task)


run(main())
```

## 😵 Help! I have a question

Create an issue and I'll try to help.

## 😡 Fix! There is something that needs improvement

Create an issue or pull request and I'll try to fix.

## 📄 License

MIT

## 🙏 Attribution

_This README was generated with ❤️ by [readme-md-generator](https://github.com/kefranabg/readme-md-generator)_
