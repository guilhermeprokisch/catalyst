from catalyst.assets import Equity
from catalyst.finance.slippage import SlippageModel
from catalyst.utils.sentinel import sentinel


class TestingSlippage(SlippageModel):
    """
    Slippage model that fills a constant number of shares per tick, for
    testing purposes.

    Parameters
    ----------
    filled_per_tick : int or TestingSlippage.ALL
        The number of shares to fill on each call to process_order. If
        TestingSlippage.ALL is passed, the entire order is filled.

    See also
    --------
    catalyst.finance.slippage.SlippageModel
    """
    ALL = sentinel('ALL')

    allowed_asset_types = (Equity,)

    def __init__(self, filled_per_tick):
        super(TestingSlippage, self).__init__()
        self.filled_per_tick = filled_per_tick

    def process_order(self, data, order):
        price = data.current(order.asset, "close")
        if self.filled_per_tick is self.ALL:
            volume = order.amount
        else:
            volume = self.filled_per_tick

        return (price, volume)
