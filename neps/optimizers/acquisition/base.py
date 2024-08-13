from abc import ABC, abstractmethod
from dataclasses import dataclass

from neps.search_spaces.search_space import SearchSpace
from neps.utils.types import Arr, Array2D, f64


@dataclass
class BaseAcquisition(ABC):
    @abstractmethod
    def __call__(self, x: Array2D[f64], space: SearchSpace) -> Arr[f64]:
        """Evaluate the acquisition function at query points"""
