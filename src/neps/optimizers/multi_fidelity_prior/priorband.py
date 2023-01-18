# type: ignore

from __future__ import annotations

import typing

import numpy as np
from typing_extensions import Literal

from ...search_spaces.search_space import SearchSpace
from ..bayesian_optimization.acquisition_functions.base_acquisition import BaseAcquisition
from ..bayesian_optimization.acquisition_samplers.base_acq_sampler import (
    AcquisitionSampler,
)
from ..multi_fidelity.hyperband import HyperbandCustomDefault
from ..multi_fidelity.mf_bo import MFBOBase
from ..multi_fidelity.promotion_policy import SyncPromotionPolicy
from ..multi_fidelity.sampling_policy import EnsemblePolicy, ModelPolicy
from .utils import compute_config_dist, compute_scores


class PriorBandBase:
    """Class that defines essential properties needed by PriorBand."""

    def find_all_distances_from_incumbent(self, incumbent):
        """Finds the distance to the nearest neighbour."""
        dist = lambda x: compute_config_dist(incumbent, x)
        # computing distance of incumbent from all seen points in history
        distances = [dist(config) for config in self.observed_configs.config]
        # ensuring the distances exclude 0 or the distance from itself
        distances = [d for d in distances if d > 0]
        return distances

    def find_1nn_distance_from_incumbent(self, incumbent):
        """Finds the distance to the nearest neighbour."""
        distances = self.find_all_distances_from_incumbent(incumbent)
        distance = min(distances)
        return distance

    def find_incumbent(self, rung: int = None) -> SearchSpace:
        """Find the best performing configuration seen so far."""
        rungs = self.observed_configs.rung.values
        idxs = self.observed_configs.index.values
        while rung is not None:
            # enters this scope is `rung` argument passed and not left empty or None
            if rung not in rungs:
                self.logger.warn(f"{rung} not in {np.unique(idxs)}")
            # filtering by rung based on argument passed
            idxs = self.observed_configs.rung.values == rung
            # checking width of current rung
            if len(idxs) < self.eta:
                self.logger.warn(
                    f"Selecting incumbent from a rung with width less than {self.eta}"
                )
        # extracting the incumbent configuration
        if len(idxs):
            # finding the config with the lowest recorded performance
            _perfs = self.observed_configs.loc[idxs].perf.values
            inc_idx = np.nanargmin([np.nan if t is None else t for t in _perfs])
            inc = self.observed_configs.loc[idxs].iloc[inc_idx].config
        else:
            # THIS block should not ever execute, but for runtime anomalies, if no
            # incumbent can be extracted, the prior is treated as the incumbent
            inc = self.pipeline_space.sample_default_configuration()
            self.logger.warn(
                "Treating the prior as the incumbent. "
                "Please check if this should not happen."
            )
        return inc

    def set_sampling_weights_and_inc(self, rung: int):
        sampling_args = self.calc_sampling_args(rung)
        if not self.is_activate_inc():
            sampling_args["prior"] += sampling_args["inc"]
            sampling_args["inc"] = 0
            inc = None
            # pylint: disable=attribute-defined-outside-init
            self.sampling_args = {"inc": inc, "weights": sampling_args}
        else:
            inc = self.find_incumbent()
            # pylint: disable=attribute-defined-outside-init
            self.sampling_args = {"inc": inc, "weights": sampling_args}
            if self.inc_sample_type == "hypersphere":
                min_dist = self.find_1nn_distance_from_incumbent(inc)
                self.sampling_args.update({"distance": min_dist})
        return self.sampling_args

    def is_activate_inc(self) -> bool:
        """Function to check optimization state to allow/disallow incumbent sampling"""
        activate_inc = False

        bracket = self.sh_brackets[self.min_rung]
        base_rung_size = bracket.config_map[bracket.min_rung]

        configs_recorded = np.count_nonzero(
            ~np.isnan(self.observed_configs.perf.values.tolist() + [np.nan])
        )

        if configs_recorded >= base_rung_size:
            activate_inc = True
        return activate_inc

    def calc_sampling_args(self, rung) -> dict:
        _w_random = 1
        # scales likelihood of a prior sample with the current rung to sample at
        _w_prior = (self.eta**rung) * _w_random

        w_prior = _w_prior / (_w_prior + _w_random)
        w_random = _w_random / (_w_prior + _w_random)

        _w_prior, _w_inc = self.prior_to_incumbent_ratio(1, 0, self.max_rung)
        w_inc = _w_inc * w_prior
        w_prior = _w_prior * w_prior

        sampling_args = {
            "prior": w_prior,
            "inc": w_inc,
            "random": w_random,
        }
        return sampling_args

    def prior_to_incumbent_ratio(self, w1: float, w2: float, rung: int) -> float | float:
        if self.inc_style == "constant":
            return self._prior_to_incumbent_ratio_constant()
        elif self.inc_style == "decay":
            return self._prior_to_incumbent_ratio_decay(w1, w2)
        elif self.inc_style == "dynamic":
            return self._prior_to_incumbent_ratio_dynamic(rung)
        else:
            raise ValueError(f"Invalid option {self.inc_style}")

    def _get_alpha(self, crossover: int = 0.5) -> float:
        nconfigs = 0
        for bracket in self.sh_brackets.values():
            nconfigs += bracket.config_map[bracket.min_rung]
        n = np.ceil((self.eta - 1) * nconfigs / self.eta)
        alpha = np.power(crossover, 1 / n)
        return alpha

    def _prior_to_incumbent_ratio_decay(self, w1: float, w2: float) -> float | float:
        """Decays the prior weightage and increases the incumbent weightage.

        The sum of weightage for prior and incumbents is always 1 here. `alpha` controls
        the rate of decay. The `crossover` point is where the weightage is equal.
        `alpha` is calculated to be such that given the HB allocations, the crossover
        will happen roughly when (eta-1) * N/eta configurations have been seen. Where,
        N is the total number of configurations sampled in 1 full HB bracket.
        """
        # 0.5 is the crossover point in between w_prior=1 and w_inc=0
        alpha = self._get_alpha(crossover=0.5)
        _w1 = w1
        _w2 = w2
        t = np.count_nonzero(
            ~np.isnan(self.observed_configs.perf.values.tolist() + [np.nan])
        )
        for _t in range(t):
            _w1 = alpha * _w1 + (1 - alpha) * w2
            _w2 = alpha * _w2 + (1 - alpha) * w1
        return _w1, _w2

    def _prior_to_incumbent_ratio_constant(self) -> float | float:
        """Fixes the weightage of incumbent sampling to 1/eta of prior sampling."""
        # fixing weight of incumbent to 1/eta of prior
        w_prior = (self.eta - 1) / self.eta
        w_inc = 1 / self.eta
        return w_prior, w_inc

    def _prior_to_incumbent_ratio_dynamic(self, rung: int) -> float | float:
        """Dynamically determines the ratio of weights for prior and incumbent sampling.

        Finds the highest rung with configurations recorded. Picks the top-eta configs
        from this rung. Each config is then ranked by performance and scored by the
        Gaussian centered around the prior configuration and the Gaussian centered around
        the current incumbent. This scores each of the top-eta configs with the
        likelihood of being sampled by the prior or the incumbent. A weighted sum is
        performed on these scores based on their ranks. The ratio of the scores is used
        as the weights for prior and incumbent sampling. These weighs are calculated
        before every sampling operation.
        """
        if len(self.rung_histories[rung]["config"]) >= self.eta:
            # retrieve the prior
            prior = self.pipeline_space.sample_default_configuration()
            # retrieve the global incumbent
            inc = self.find_incumbent()
            # ranking by performance
            config_idxs = np.argsort(self.rung_histories[rung]["perf"])[: self.eta]
            # find the top-eta configurations in the rung
            top_configs = np.array(self.rung_histories[rung]["config"])[config_idxs]
            top_config_scores = np.array(
                [
                    # `compute_scores` returns a tuple of scores resp. by prior and inc
                    compute_scores(
                        self.observed_configs.loc[config_id].config, prior, inc
                    )
                    for config_id in top_configs
                ]
            )
            # adding positional weights to the score, with the best config weighed most
            weights = np.flip(np.arange(1, top_config_scores.shape[0] + 1)).reshape(-1, 1)
            # calculating sum of weights
            weighted_top_config_scores = np.sum(top_config_scores * weights, axis=0)
            prior_score, inc_score = weighted_top_config_scores
            # normalizing scores to be weighted ratios
            w_prior = prior_score / sum(weighted_top_config_scores)
            w_inc = inc_score / sum(weighted_top_config_scores)
        else:
            # if no configurations recorded yet
            # check if it is the base rung which is empty
            if rung == self.min_rung:
                w_prior = 1
                w_inc = 0
            else:
                # if rung > min.rung then the lower rung could already have enough
                # configurations and thus can be recursively queried till the base rung
                return self._prior_to_incumbent_ratio_dynamic(rung - 1)
        return w_prior, w_inc


# order of inheritance (method resolution order) extremely essential for correct behaviour
class PriorBand(MFBOBase, HyperbandCustomDefault, PriorBandBase):
    def __init__(
        self,
        pipeline_space: SearchSpace,
        budget: int,
        eta: int = 3,
        initial_design_type: Literal["max_budget", "unique_configs"] = "max_budget",
        sampling_policy: typing.Any = EnsemblePolicy,
        promotion_policy: typing.Any = SyncPromotionPolicy,
        loss_value_on_error: None | float = None,
        cost_value_on_error: None | float = None,
        ignore_errors: bool = False,
        logger=None,
        prior_confidence: Literal["low", "medium", "high"] = "medium",
        random_interleave_prob: float = 0.0,
        sample_default_first: bool = True,
        inc_sample_type: str = "hypersphere",  # could also be {"gaussian", "crossover"}
        inc_style: str = "constant",  # could also be {"decay", "dynamic"}
        # arguments for model
        model_based: bool = False,  # crucial argument to set to allow model-search
        model_policy: typing.Any = ModelPolicy,
        surrogate_model: str | typing.Any = "gp",
        domain_se_kernel: str = None,
        hp_kernels: list = None,
        surrogate_model_args: dict = None,
        acquisition: str | BaseAcquisition = "EI",
        log_prior_weighted: bool = False,
        acquisition_sampler: str | AcquisitionSampler = "random",
    ):
        super().__init__(
            pipeline_space=pipeline_space,
            budget=budget,
            eta=eta,
            initial_design_type=initial_design_type,
            sampling_policy=sampling_policy,
            promotion_policy=promotion_policy,
            loss_value_on_error=loss_value_on_error,
            cost_value_on_error=cost_value_on_error,
            ignore_errors=ignore_errors,
            logger=logger,
            prior_confidence=prior_confidence,
            random_interleave_prob=random_interleave_prob,
            sample_default_first=sample_default_first,
        )
        self.inc_sample_type = inc_sample_type
        self.sampling_policy = sampling_policy(
            pipeline_space=pipeline_space, inc_type=self.inc_sample_type
        )
        # determines the kind of trade-off between incumbent and prior weightage
        self.inc_style = inc_style  # used by PriorBandBase
        self.sampling_args = {
            "inc": None,
            "weights": {
                "prior": 1,  # begin with only prior sampling
                "inc": 0,
                "random": 0,
            },
        }

        bo_args = dict(
            surrogate_model=surrogate_model,
            domain_se_kernel=domain_se_kernel,
            hp_kernels=hp_kernels,
            surrogate_model_args=surrogate_model_args,
            acquisition=acquisition,
            log_prior_weighted=log_prior_weighted,
            acquisition_sampler=acquisition_sampler,
        )
        self.model_based = model_based
        # counting non-fidelity dimensions in search space
        ndims = sum(
            
                1
                for _, hp in self.pipeline_space.hyperparameters.items()
                if not hp.is_fidelity
            
        )
        n_min = ndims + 1
        self.init_size = n_min + 1  # in BOHB: init_design >= N_min + 2
        self.model_policy = model_policy(pipeline_space, **bo_args)

        for _, sh in self.sh_brackets.items():
            sh.sampling_policy = self.sampling_policy
            sh.sampling_args = self.sampling_args
            sh.model_policy = self.model_policy
            sh.sample_new_config = self.sample_new_config

    def get_config_and_ids(  # pylint: disable=no-self-use
        self,
    ) -> tuple[SearchSpace, str, str | None]:
        """...and this is the method that decides which point to query.

        Returns:
            [type]: [description]
        """
        print(f"Rung={self.current_sh_bracket}")
        self.set_sampling_weights_and_inc(rung=self.current_sh_bracket)

        for _, sh in self.sh_brackets.items():
            sh.sampling_args = self.sampling_args
        print(sh.sampling_args["weights"])  # pylint: disable=undefined-loop-variable
        return super().get_config_and_ids()


class PriorBandNoIncToPrior(PriorBand):
    """Disables incumbent sampling to replace with prior-based sampling."""

    def set_sampling_weights_and_inc(self, rung: int):
        super().set_sampling_weights_and_inc(rung)
        self.sampling_args["weights"]["prior"] += self.sampling_args["weights"]["inc"]
        self.sampling_args["weights"]["inc"] = 0

        return self.sampling_args


class PriorBandNoPriorToInc(PriorBand):
    """Disables prior based sampling to replace with incumbent-based sampling."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # cannot use prior in this version
        self.pipeline_space.has_prior = False

    def set_sampling_weights_and_inc(self, rung: int):
        super().set_sampling_weights_and_inc(rung)
        # setting the incumbent weight to eta times of random when in-sampling activated
        if self.sampling_args["weights"]["inc"] > 0:
            self.sampling_args["weights"]["random"] = self.eta / (1 + self.eta)
            self.sampling_args["weights"]["inc"] = 1 / (1 + self.eta)
        else:
            self.sampling_args["weights"]["random"] = 1
        self.sampling_args["weights"]["prior"] = 0
        return self.sampling_args
