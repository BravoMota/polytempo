"""Strategy evaluation package."""

from polytempo.strategy.argmax_no import ArgmaxNoStrategy
from polytempo.strategy.argmax_yes import ArgmaxYesStrategy
from polytempo.strategy.base import Strategy
from polytempo.strategy.book_arb import BookArbConfig, BookArbStrategy
from polytempo.strategy.coverage_band import CoverageBandConfig, CoverageBandStrategy
from polytempo.strategy.dist_arb import DistArbConfig, DistArbStrategy
from polytempo.strategy.dist_arb_kelly import DistArbKellyConfig, DistArbKellyStrategy
from polytempo.strategy.dist_arb_tight import DistArbTightConfig, DistArbTightStrategy
from polytempo.strategy.edge_band import EdgeBandConfig, EdgeBandStrategy
from polytempo.strategy.max_edge import MaxEdgeStrategy
from polytempo.strategy.max_roi import MaxRoiConfig, MaxRoiStrategy
from polytempo.strategy.mid_band import MidBandConfig, MidBandStrategy
from polytempo.strategy.tail_fade import TailFadeConfig, TailFadeStrategy
from polytempo.strategy.topk import TopKStrategy

__all__ = [
    "Strategy",
    "ArgmaxYesStrategy",
    "ArgmaxNoStrategy",
    "BookArbStrategy",
    "BookArbConfig",
    "CoverageBandStrategy",
    "CoverageBandConfig",
    "DistArbStrategy",
    "DistArbConfig",
    "DistArbKellyStrategy",
    "DistArbKellyConfig",
    "DistArbTightStrategy",
    "DistArbTightConfig",
    "MaxRoiStrategy",
    "MaxRoiConfig",
    "MidBandStrategy",
    "MidBandConfig",
    "TailFadeStrategy",
    "TailFadeConfig",
    "TopKStrategy",
    "MaxEdgeStrategy",
    "EdgeBandStrategy",
    "EdgeBandConfig",
]
