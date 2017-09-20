from datetime import timedelta

from time import sleep

import os
import pandas as pd
from catalyst.data.bundles.base_pricing import BaseCryptoPricingBundle

from catalyst import get_calendar
from logbook import Logger, INFO

from catalyst.data.five_minute_bars import BcolzFiveMinuteOverlappingData
from catalyst.data.minute_bars import BcolzMinuteOverlappingData, \
    BcolzMinuteBarReader
from catalyst.exchange.bitfinex.bitfinex import Bitfinex
from catalyst.exchange.bittrex.bittrex import Bittrex
from catalyst.exchange.exchange_errors import ExchangeNotFoundError
from catalyst.exchange.exchange_utils import get_exchange_auth
from catalyst.utils.cli import maybe_show_progress


def _cachpath(symbol, type_):
    return '-'.join([symbol, type_])


log = Logger('exchange_bundle')


def fetch_candles_chunk(exchange, assets, data_frequency, end_dt, bar_count):
    candles = exchange.get_candles(
        data_frequency=data_frequency,
        assets=assets,
        bar_count=bar_count,
        end_dt=end_dt
    )

    series = dict()

    for asset in assets:
        asset_candles = candles[asset]

        asset_df = pd.DataFrame(asset_candles)
        if not asset_df.empty:
            asset_df.set_index('last_traded', inplace=True, drop=True)
            asset_df.sort_index(inplace=True)

            series[asset] = asset_df

    return series


def process_bar_data(exchange, assets, writer, data_frequency,
                     show_progress, start, end):
    open_calendar = get_calendar('OPEN')

    writer.calendar = open_calendar
    writer.minutes_per_day = 1440
    writer.write_metadata = True

    delta = end - start
    if data_frequency == 'minute':
        delta_periods = delta.total_seconds() / 60
        frequency = '1m'

    elif data_frequency == '5-minute':
        delta_periods = delta.total_seconds() / 60 / 5
        frequency = '5m'

    elif data_frequency == 'daily':
        delta_periods = delta.total_seconds() / 60 / 60 / 24
        frequency = '1d'

    else:
        raise ValueError('frequency not supported')

    if delta_periods > exchange.num_candles_limit:
        bar_count = exchange.num_candles_limit

        chunks = []
        last_chunk_date = end
        while last_chunk_date > start + timedelta(minutes=bar_count):
            # TODO: account for the partial last bar
            chunk = dict(end=last_chunk_date, bar_count=bar_count)
            chunks.append(chunk)

            last_chunk_date = \
                last_chunk_date - timedelta(minutes=(bar_count + 1))

        chunks.reverse()

    else:
        chunks = [dict(end=end, bar_count=delta_periods)]

    with maybe_show_progress(
            chunks,
            show_progress,
            label='Fetching {exchange} {frequency} candles: '.format(
                exchange=exchange.name,
                frequency=data_frequency
            )) as it:

        for chunk in it:
            assets_candles_dict = fetch_candles_chunk(
                exchange=exchange,
                assets=assets,
                data_frequency=frequency,
                end_dt=chunk['end'],
                bar_count=chunk['bar_count']
            )
            log.debug('requests counter {}'.format(exchange.request_cpt))

            if not assets_candles_dict.keys():
                log.debug(
                    'no data: {symbols} on {exchange}, date {end}'.format(
                        symbols=assets,
                        exchange=exchange.name,
                        end=chunk['end']
                    )
                )
                continue

            data = []
            for asset in assets_candles_dict:
                df = assets_candles_dict[asset]
                sid = asset.sid
                data.append((sid, df))

            try:
                log.debug(
                    'writing chunk {start} to {end}'.format(
                        start=chunk['end'] - timedelta(
                            minutes=chunk['bar_count']),
                        end=chunk['end']
                    )
                )
                writer.write(
                    data=data,
                    show_progress=False,
                    invalid_data_behavior='raise'
                )
            except (BcolzMinuteOverlappingData,
                    BcolzFiveMinuteOverlappingData) as e:
                log.warn('chunk already exists {}: {}'.format(chunk, e))


def exchange_bundle(exchange_name, symbols=None, start=None, end=None,
                    log_level=INFO):
    """Create a data bundle ingest function for the specified exchange.

    Parameters
    ----------
    exchange_name: str
        The name of the exchange
    symbols : iterable[str]
        The ticker symbols to load data for.
    start : datetime, optional
        The start date to query for. By default this pulls the full history
        for the calendar.
    end : datetime, optional
        The end date to query for. By default this pulls the full history
        for the calendar.

    Returns
    -------
    ingest : callable
        The bundle ingest function for the given set of symbols.

    Examples
    --------
    This code should be added to ~/.catalyst/extension.py

    .. code-block:: python

       from catalyst.data.bundles import register
       from catalyst.exchange.exchange_bundle import exchange_bundle

       symbols = (
           'btc_usd',
           'eth_btc',
           'etc_btc',
           'neo_btc',
       )
       register('exchange_bitfinex', exchange_bundle('bitfinex', symbols))

    Notes
    -----
    The sids for each symbol will be the index into the symbols sequence.
    """
    # strict this in memory so that we can reiterate over it
    log.level = log_level

    def ingest(environ,
               asset_db_writer,
               minute_bar_writer,
               five_minute_bar_writer,
               daily_bar_writer,
               adjustment_writer,
               calendar,
               start_session,
               end_session,
               cache,
               show_progress,
               is_compile,
               output_dir,
               start=start,
               end=end):

        log.info('ingesting bundle {}'.format(output_dir))

        # TODO: I don't understand this session vs dates idea
        if start is None:
            start = start_session
        if end is None:
            end = end_session

        now = pd.Timestamp.utcnow()
        if end > now:
            log.info('adjusting the end date to now {}'.format(now))
            end = now

        log.info('ingesting data from {} to {}'.format(start, end))

        exchange_auth = get_exchange_auth(exchange_name)
        if exchange_name == 'bitfinex':
            exchange = Bitfinex(
                key=exchange_auth['key'],
                secret=exchange_auth['secret'],
                base_currency=None,  # TODO: make optional at the exchange
                portfolio=None
            )
        elif exchange_name == 'bittrex':
            exchange = Bittrex(
                key=exchange_auth['key'],
                secret=exchange_auth['secret'],
                base_currency=None,
                portfolio=None
            )
        else:
            raise ExchangeNotFoundError(exchange_name=exchange_name)

        if symbols is not None:
            assets = exchange.get_assets(symbols)
        else:
            assets = exchange.assets

        earliest_trade = None
        for asset in assets:
            if earliest_trade is None or earliest_trade > asset.start_date:
                earliest_trade = asset.start_date

        if earliest_trade > start:
            log.info(
                'adjusting start date to earliest trade date found {}'.format(
                    earliest_trade
                ))
            start = earliest_trade

        if start >= end:
            raise ValueError('start date cannot be after end date')

        if daily_bar_writer is not None:
            process_bar_data(
                exchange=exchange,
                assets=assets,
                writer=daily_bar_writer,
                data_frequency='daily',
                show_progress=show_progress,
                start=start,
                end=end
            )

        if five_minute_bar_writer is not None:
            process_bar_data(
                exchange=exchange,
                assets=assets,
                writer=five_minute_bar_writer,
                data_frequency='5-minute',
                show_progress=show_progress,
                start=start,
                end=end
            )

        if minute_bar_writer is not None:
            process_bar_data(
                exchange=exchange,
                assets=assets,
                writer=minute_bar_writer,
                data_frequency='minute',
                show_progress=show_progress,
                start=start,
                end=end
            )

    return ingest

