"Test bounded channels"

from asyncio import create_task, gather, sleep
from time import time

import pytest

from option_and_result import MatchesErr, MatchesOk
from channel import Receiver, Sender, channel


async def producer_finishes_first(sender: Sender[int]):
    "Send values 10, 17, ..., 94 faster than the consumer can receive/process them all"

    value = 3
    while True:
        value += 7

        if value >= 100:
            break

        res = await sender.send(value)

        match res.to_matchable():
            case MatchesErr(send_error):
                assert (
                    False
                ), f"channel was closed before trying to send {send_error.value}"


async def consumer_is_slower(receiver: Receiver[int]):
    """
    Receive values 10, 17, ..., 94 slower than they are produced
    (because of deliberately sleeping to mimic intensive work)
    """

    started = time()
    actual_values = []
    sleep_time = 0.1
    async for value in receiver:
        actual_values.append(value)
        await sleep(sleep_time)
    actual_length = len(actual_values)
    ended = time()
    elapsed = ended - started

    expected_values = [10, 17, 24, 31, 38, 45, 52, 59, 66, 73, 80, 87, 94]
    expected_length = len(expected_values)

    assert actual_values == expected_values

    assert elapsed > sleep_time * expected_length
    assert elapsed > sleep_time * actual_length


@pytest.mark.asyncio
async def test_producer_finishes_first():
    "Test that things behave as expected when the producer finishes before the consumer"

    (sender, receiver) = channel(4)

    producer_task = create_task(producer_finishes_first(sender))
    consumer_task = create_task(consumer_is_slower(receiver))

    # Remove extra references so RAII can work correctly
    del sender
    del receiver

    await gather(producer_task, consumer_task)


CONSUMER_FINISHES_FIRST_EXPECTED_VALUES = [
    99,
    98,
    97,
    96,
    95,
    94,
    93,
    92,
    91,
    90,
    89,
    88,
    87,
    86,
    85,
    84,
    83,
    82,
    81,
    80,
]
CONSUMER_FINISHES_FIRST_BUFFER = 16


async def producer_is_slower(sender: Sender[int]):
    """
    Produce values 99, 98, 97, ... until the receiver is dropped (in this case at 80)
    and slower than the consumer receives / processes them
    """

    started = time()
    actual_values = []
    sleep_time = 0.05

    decrement = 1
    value = 100
    while True:
        value -= decrement

        await sleep(sleep_time)

        res = await sender.send(value)

        match res.to_matchable():
            case MatchesErr(send_error):
                assert (
                    send_error.value
                    == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES[-1] - decrement
                )
                break
            case MatchesOk(None):
                actual_values.append(value)
    actual_length = len(actual_values)
    ended = time()
    elapsed = ended - started

    expected_length = len(CONSUMER_FINISHES_FIRST_EXPECTED_VALUES)

    assert elapsed > sleep_time * actual_length
    assert elapsed > sleep_time * expected_length

    assert actual_values == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES


async def consumer_finishes_first(receiver: Receiver[int]):
    """
    Receive values until the expected number of them (20) are received
    and at a rate faster than the producer can send them
    """

    expected_length = len(CONSUMER_FINISHES_FIRST_EXPECTED_VALUES)

    started = time()
    actual_values = []
    sleep_time = 0.02
    i = 0
    async for value in receiver:
        actual_values.append(value)
        i += 1
        if i == expected_length:
            break
        await sleep(sleep_time)
    # Ensure references are dropped for proper RAII behavior
    del receiver
    # Probably optional given the rest of this function is synchronous anyway:
    actual_length = len(actual_values)
    ended = time()

    elapsed = ended - started

    assert elapsed > sleep_time * actual_length
    assert elapsed > sleep_time * expected_length

    assert actual_values == CONSUMER_FINISHES_FIRST_EXPECTED_VALUES


@pytest.mark.asyncio
async def test_consumer_finishes_first():
    "Test that things behave as expected when the consumer finishes before the producer"
    (sender, receiver) = channel(CONSUMER_FINISHES_FIRST_BUFFER)

    producer_task = create_task(producer_is_slower(sender))
    consumer_task = create_task(consumer_finishes_first(receiver))

    # Remove extra references so RAII can work correctly
    del sender
    del receiver

    await gather(producer_task, consumer_task)
