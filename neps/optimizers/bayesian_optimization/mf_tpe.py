from __future__ import annotations

import random
from typing import Iterable

import numpy as np
import torch
from metahyper.api import ConfigResult, instance_from_map
from scipy.stats import spearmanr
from typing_extensions import Literal

from ...search_spaces import CategoricalParameter, FloatParameter, IntegerParameter
from ...search_spaces.search_space import SearchSpace
from .. import BaseOptimizer
from .acquisition_samplers import AcquisitionSamplerMapping
from .acquisition_samplers.base_acq_sampler import AcquisitionSampler
from .models import SurrogateModelMapping


class MultiFidelityPriorWeightedTreeParzenEstimator(BaseOptimizer):
    def __init__(
        self,
        pipeline_space: SearchSpace,
        use_priors: bool = False,
        prior_num_evals: float = 1.0,
        good_fraction: float = 0.3334,
        random_interleave_prob: float = 0.3334,
        initial_design_size: int = 0,
        prior_as_samples: bool = True,
        pending_as_bad: bool = True,
        fidelity_weighting: Literal["linear", "spearman"] = "spearman",
        surrogate_model: str = "kde",
        acquisition_sampler: str | AcquisitionSampler = "mutation",
        prior_draws: int = 1000,
        surrogate_model_args: dict = None,
        soft_promotion: bool = True,
        patience: int = 50,
        logger=None,
        budget: None | int | float = None,
        loss_value_on_error: None | float = None,
        cost_value_on_error: None | float = None,
    ):
        """[summary]

        Args:
            pipeline_space: Space in which to search
            prior_num_evals (float, optional): [description]. Defaults to 1.0.
            good_fraction (float, optional): [description]. Defaults to 0.333.
            random_interleave_prob: Frequency at which random configurations are sampled
                instead of configurations from the acquisition strategy.
            initial_design_size: Number of 'x' samples that are to be evaluated before
                selecting a sample using a strategy instead of randomly. If there is a
                user prior, we can rely on the model from the very first iteration.
            prior_as_samples: Whether to sample from the KDE and incorporate that way, or
            just have the distribution be an linear combination of the KDE and the prior.
            Should be True if the prior happens to be unnormalized.
            pending_as_bad: Whether to treat pending observations as bad, assigning them to
            the bad KDE to encourage diversity among samples queried in parallel
            prior_draws: The number of samples drawn from the prior if there is one. This
            # does not affect the strength of the prior, just how accurately it
            # is reconstructed by the KDE.
            patience: How many times we try something that fails before giving up.
            budget: Maximum budget
            loss_value_on_error: Setting this and cost_value_on_error to any float will
                supress any error during bayesian optimization and will use given loss
                value instead. default: None
            cost_value_on_error: Setting this and loss_value_on_error to any float will
                supress any error during bayesian optimization and will use given cost
                value instead. default: None
            logger: logger object, or None to use the neps logger
        """
        super().__init__(
            pipeline_space=pipeline_space,
            patience=patience,
            logger=logger,
            budget=budget,
            loss_value_on_error=loss_value_on_error,
            cost_value_on_error=cost_value_on_error,
        )
        self.pipeline_space = pipeline_space
        if self.pipeline_space.has_fidelity:
            self.min_fidelity = pipeline_space.fidelity.lower
            self.max_fidelity = pipeline_space.fidelity.upper

        else:
            self.min_fidelity = 1
            self.max_fidelity = 1
        if initial_design_size == 0:
            self._initial_design_size = len(self.pipeline_space) + 1
        else:
            self._initial_design_size = initial_design_size

        self.num_fidelities = int(self.max_fidelity) + 1 - int(self.min_fidelity)
        self.use_priors = use_priors
        self.prior_num_evals = prior_num_evals
        self.good_fraction = good_fraction
        self._random_interleave_prob = random_interleave_prob
        self._pending_as_bad = pending_as_bad
        self.prior_draws = prior_draws
        self._has_promotable_configs = False
        self.soft_promotion = soft_promotion
        # if we use priors, we don't add conigurations as good until is is within the top fraction
        # This heuristic has not been tried further, but makes sense in the context when we have priors
        self.round_up = not use_priors
        self.fidelity_weighting = fidelity_weighting

        # TODO have this read in as part of load_results - it cannot be saved as an attribute when
        # running parallel instances of the algorithm (since the old configs are shared, not instance-specific)
        self.old_configs_per_fid = [[] for i in range(self.num_fidelities)]
        # We assume that the information conveyed per fidelity (and the cost) is linear in the
        # fidelity levels if nothing else is specified
        if surrogate_model != "kde":
            raise NotImplementedError(
                "Only supports KDEs for now. Could (maybe?) support binary classification in the future."
            )
        self.acquisition_sampler = instance_from_map(
            AcquisitionSamplerMapping,
            acquisition_sampler,
            name="acquisition sampler function",
            kwargs={"patience": self.patience, "pipeline_space": self.pipeline_space},
        )
        surrogate_model_args = surrogate_model_args or {}

        param_types, num_options, logged_params, is_fidelity = self._get_types()
        surrogate_model_args["param_types"] = param_types
        surrogate_model_args["num_options"] = num_options
        surrogate_model_args["is_fidelity"] = is_fidelity
        surrogate_model_args["logged_params"] = logged_params

        # TODO consider the logged versions of parameters
        if self.pipeline_space.has_prior and use_priors:
            if prior_as_samples:
                self.prior_samples = [
                    self.pipeline_space.sample(
                        patience=self.patience, user_priors=True, ignore_fidelity=False
                    )
                    for idx in range(self.prior_draws)
                ]
            else:
                pass
                # TODO work out affine combination
        else:
            self.prior_samples = []

        self.surrogate_models = {
            "good": instance_from_map(
                SurrogateModelMapping,
                surrogate_model,
                name="surrogate model",
                kwargs=surrogate_model_args,
            ),
            "bad": instance_from_map(
                SurrogateModelMapping,
                surrogate_model,
                name="surrogate model",
                kwargs=surrogate_model_args,
            ),
            "all": instance_from_map(
                SurrogateModelMapping,
                surrogate_model,
                name="surrogate model",
                kwargs=surrogate_model_args,
            ),
        }
        self.acquisition = self
        self.acquisition_sampler = instance_from_map(
            AcquisitionSamplerMapping,
            acquisition_sampler,
            name="acquisition sampler function",
            kwargs={"patience": self.patience, "pipeline_space": self.pipeline_space},
        )

    def _get_types(self):
        """extracts the needed types from the configspace for faster retrival later

        type = 0 - numerical (continuous or integer) parameter
        type >=1 - categorical parameter

        TODO: figure out a way to properly handle ordinal parameters

        """
        types = []
        num_values = []
        logs = []
        is_fidelity = []
        for _, hp in self.pipeline_space.items():
            is_fidelity.append(hp.is_fidelity)
            if isinstance(hp, CategoricalParameter):
                # u as in unordered - used to play nice with the statsmodels KDE implementation
                types.append("u")
                logs.append(False)
                num_values.append(len(hp.choices))
            elif isinstance(hp, IntegerParameter):
                # o as in ordered
                types.append("o")
                logs.append(False)
                num_values.append(hp.upper - hp.lower + 1)
            elif isinstance(hp, FloatParameter):
                # c as in continous
                types.append("c")
                logs.append(hp.log)
                num_values.append(np.inf)

            else:
                raise ValueError("Unsupported Parametertype %s" % type(hp))

        return types, num_values, logs, is_fidelity

    def __call__(
        self, x: Iterable, asscalar: bool = False, only_lowest_fidelity=True
    ) -> np.ndarray | torch.Tensor | float:
        """
        Return the negative probability of / expected improvement at the query point
        """
        # this is to only make the lowest fidelity viable
        # TODO have this as a setting in the acq_sampler instead
        if only_lowest_fidelity:
            is_lowest_fidelity = (
                np.array([x_.fidelity.value for x_ in x]) == self.min_fidelity
            )
            return (
                is_lowest_fidelity
                * self.surrogate_models["good"].pdf(x)
                / self.surrogate_models["bad"].pdf(x)
            )
        else:
            return self.surrogate_models["good"].pdf(x) / self.surrogate_models[
                "bad"
            ].pdf(x)

    # TODO allow this for integers as well - now only supports floats

    def _convert_to_logscale(self):
        pass

    def _split_by_fidelity(self, configs, losses):
        min_fid, max_fid = int(self.min_fidelity), int(self.max_fidelity)
        if self.pipeline_space.has_fidelity:

            configs_per_fidelity = [[] for i in range(min_fid, max_fid + 1)]
            losses_per_fidelity = [[] for i in range(min_fid, max_fid + 1)]
            # per fidelity, add a list to make it a nested list of lists
            # [[config_A at fid1, config_B at fid1], [config_C at fid2], ...]
            for config, loss in zip(configs, losses):
                configs_per_fidelity[int(config.fidelity.value - min_fid)].append(config)
                losses_per_fidelity[int(config.fidelity.value - min_fid)].append(loss)
            return configs_per_fidelity, losses_per_fidelity
        else:
            return [configs], [losses]

    def _split_configs(
        self, configs_per_fid, losses_per_fid, weight_per_fidelity, good_fraction=None
    ):
        """Splits configs into good and bad for the KDEs.

        Args:
            configs ([type]): [description]
            losses ([type]): [description]
            round_up (bool, optional): [description]. Defaults to True.

        Returns:
            [type]: [description]
        """
        if good_fraction is None:
            good_fraction = self.good_fraction

        good_configs, bad_configs = [], []
        good_configs_weights, bad_configs_weights = [], []

        for fid, (configs_fid, losses_fid) in enumerate(
            zip(configs_per_fid, losses_per_fid)
        ):
            if self.round_up:
                num_good_configs = np.ceil(len(configs_fid) * good_fraction).astype(int)
            else:
                num_good_configs = np.floor(len(configs_fid) * good_fraction).astype(int)

            ordered_losses = np.argsort(losses_fid)
            good_indices = ordered_losses[0:num_good_configs]
            bad_indices = ordered_losses[num_good_configs:]
            good_configs_fid = [configs_fid[idx] for idx in good_indices]
            bad_configs_fid = [configs_fid[idx] for idx in bad_indices]
            good_configs.extend(good_configs_fid)
            bad_configs.extend(bad_configs_fid)
            good_configs_weights.extend(
                [weight_per_fidelity[fid]] * len(good_configs_fid)
            )
            bad_configs_weights.extend([weight_per_fidelity[fid]] * len(bad_configs_fid))
        return good_configs, bad_configs, good_configs_weights, bad_configs_weights

    def compute_fidelity_weights(self, configs_per_fid, losses_per_fid) -> list:
        # TODO consider pending configurations - will default to a linear weighting
        # which is not necessarily correct
        if self.fidelity_weighting == "linear":
            weight_per_fidelity = self._compute_linear_weights()
        elif self.fidelity_weighting == "spearman":
            weight_per_fidelity = self._compute_spearman_weights(
                configs_per_fid, losses_per_fid
            )
        else:
            raise ValueError(
                f"No weighting scheme {self.fidelity_weighting} is available."
            )
        return weight_per_fidelity

    def _compute_linear_weights(self):
        return np.arange(self.min_fidelity, self.max_fidelity + 1) / self.max_fidelity

    def _compute_spearman_weights(self, configs_per_fid, losses_per_fid) -> list:
        min_number_samples = np.round(1 / self.good_fraction).astype(int)
        samples_per_fid = np.array([len(cfgs_fid) for cfgs_fid in configs_per_fid])
        max_comparable_fid = (
            self.max_fidelity
            - 1
            - np.argmax(np.flip(samples_per_fid) >= min_number_samples)
        ).astype(int)
        if max_comparable_fid == 0:
            # if we cannot compare to any otḧer fidelity, return default
            return self._compute_linear_weights()
        else:
            # get the ranking of the configurations at the top fidelity
            comp_configs = configs_per_fid[max_comparable_fid]

            # ranks the configs at the top comparable fidelity 1 to N
            comp_losses = losses_per_fid[max_comparable_fid]

            # compare the rankings of the existing configurations to the ranking
            # of the same configurations at lower rungs
            spearman = np.ones(self.num_fidelities)
            for fid_idx, (cfgs, losses) in enumerate(
                zip(configs_per_fid, losses_per_fid)
            ):
                lower_fid_configs = [None] * len(comp_configs)
                lower_fid_losses = [None] * len(comp_configs)
                for cfg, loss in zip(cfgs, losses):
                    # check if the config at the lower fidelity level is in the comparison set
                    # TODO make this more efficient - probably embarrasingly slof for now
                    # with the triple-nested loop (although number of configs per level is pretty low)
                    is_equal_config = [
                        cfg.is_equal_value(comp_cfg, include_fidelity=False)
                        for comp_cfg in comp_configs
                    ]
                    if any(is_equal_config):
                        equal_index = np.argmax(is_equal_config)
                        lower_fid_configs[equal_index] = cfg
                        lower_fid_losses[equal_index] = loss
                # THose fidelities
                # print('fid', fid_idx)
                # print('lower_fid_losses', lower_fid_losses)
                # print('comp_losses', comp_losses)
                spearman[fid_idx] = spearmanr(lower_fid_losses, comp_losses).correlation
                if fid_idx == max_comparable_fid:
                    break

            spearman = np.clip(spearman, a_min=0, a_max=1)
            # The correlation with Z_max at fidelity Z-k cannot be larger than at Z-k+1
            spearman = np.flip(np.minimum.accumulate(np.flip(spearman)))
            fidelity_weights = spearman * max_comparable_fid / self.max_fidelity
        return fidelity_weights

    def is_init_phase(self) -> bool:
        """Decides if optimization is still under the warmstart phase/model-based search."""
        if self._num_train_x >= self._initial_design_size:
            return False
        return True

    def load_results(
        self,
        previous_results: dict[str, ConfigResult],
        pending_evaluations: dict[str, ConfigResult],
    ) -> None:
        # TODO remove doubles from previous results
        train_y = [self.get_loss(el.result) for el in previous_results.values()]

        train_x_configs = [el.config for el in previous_results.values()]
        pending_configs = list(pending_evaluations.values())

        filtered_configs, filtered_indices = self._filter_old_configs(train_x_configs)
        filtered_y = np.array(train_y)[filtered_indices].tolist()

        self.train_x_configs = train_x_configs
        self._pending_evaluations = pending_evaluations
        self._num_train_x = len(self.train_x_configs)
        if not self.is_init_phase():
            # This is to extract the configurations as numpy arrays on the format num_data x num_dim
            # TODO when a config is removed in the filtering process, that means that some other
            # configuration at the lower fidelity will become good, that was previously bad. This
            # may be good or bad, but I'm not sure. / Carl
            configs_per_fid, losses_per_fid = self._split_by_fidelity(
                train_x_configs, train_y
            )
            filtered_configs_per_fid, filtered_losses_per_fid = self._split_by_fidelity(
                filtered_configs, filtered_y
            )
            weight_per_fidelity = self.compute_fidelity_weights(
                configs_per_fid, losses_per_fid
            )
            good_configs, bad_configs, good_weights, bad_weights = self._split_configs(
                filtered_configs_per_fid, filtered_losses_per_fid, weight_per_fidelity
            )
            if self.use_priors:
                num_prior_configs = len(self.prior_samples)
                good_configs.extend(self.prior_samples)
                prior_sample_constant = self.prior_num_evals / num_prior_configs
                good_weights.extend([prior_sample_constant] * num_prior_configs)
            # TODO drop the fidelity!
            self.surrogate_models["all"].fit(filtered_configs)
            fixed_bw = self.surrogate_models["all"].bw

            self.surrogate_models["good"].fit(
                good_configs, fixed_bw=fixed_bw, config_weights=good_weights
            )
            if self._pending_as_bad:
                # This is only to compute the weights of the pending configs
                _, pending_configs, _, pending_weights = self._split_configs(
                    pending_configs,
                    [np.inf] * len(pending_configs),
                    weight_per_fidelity,
                    good_fraction=0.0,
                )
                bad_configs.extend(pending_configs)
                bad_weights.extend(pending_weights)

            self.surrogate_models["bad"].fit(
                bad_configs, fixed_bw=fixed_bw, config_weights=bad_weights
            )

    def _filter_old_configs(self, configs):
        new_configs = []
        new_indices = []
        old_configs_flat = []
        for cfgs in self.old_configs_per_fid:
            old_configs_flat.extend(cfgs)

        for idx, cfg in enumerate(configs):
            if any([cfg.is_equal_value(old_cfg) for old_cfg in old_configs_flat]):
                # If true, configs are equal and shouldn't be added
                continue
            else:
                new_configs.append(cfg)
                new_indices.append(idx)
        return new_configs, new_indices

    def _get_promotable_configs(self, configs):
        if self.soft_promotion:
            configs_for_promotion = self._get_soft_promotable(configs)
        else:
            configs_for_promotion = self._get_hard_promotable(configs)
        return configs_for_promotion

    def _get_hard_promotable(self, configs):
        # count the number of configs that are at or above any given rung
        configs_per_rung = np.zeros(self.num_fidelities)
        # check the number of configs per fidelity level
        for config in configs:
            configs_per_rung[int(config.fidelity.value - self.min_fidelity)] += 1

        cumulative_per_rung = np.flip(np.cumsum(np.flip(configs_per_rung)))
        cumulative_above = np.append(np.flip(np.cumsum(np.flip(configs_per_rung[1:]))), 0)
        # then check which one can make the most informed decision on promotions
        rungs_to_promote = cumulative_per_rung * self.good_fraction - cumulative_above

        # this defaults to max_fidelity if there is no promotable config (cannot promote from)
        # the top fidelity anyway
        fid_to_promote = self.num_fidelities - np.argmax(np.flip(rungs_to_promote) > 1)

        # TODO check if this returns empty when it needs to
        if fid_to_promote == self.max_fidelity:
            return []
        return [cfg for cfg in configs if cfg.fidelity.value == fid_to_promote]

    def _get_soft_promotable(self, configs):
        # TODO implement
        # count the number of configs that are at or above any given rung
        new_configs, _ = self._filter_old_configs(configs)
        configs_per_rung = np.zeros(self.num_fidelities)

        # check the number of configs per fidelity level
        for config in new_configs:
            configs_per_rung[int(config.fidelity.value - self.min_fidelity)] += 1
        rungs_to_promote = configs_per_rung * np.power(
            self.good_fraction, np.flip(np.sqrt(np.arange(self.num_fidelities)))
        )
        # import time
        # time.sleep(1)
        rungs_to_promote[-1] = 0
        fids_to_promote = (
            np.arange(self.num_fidelities)[rungs_to_promote > 1] + self.min_fidelity
        )

        if len(fids_to_promote) == 0:
            return []
        return [cfg for cfg in new_configs if cfg.fidelity.value in fids_to_promote]

    def _promote_existing(self, configs_for_promotion):
        # TODO we still need to REMOVE the observation at the lower fidelity
        # i.e. give it zero weight in the KDE, and ensure the count is correct
        assert len(configs_for_promotion) > 0, "No promotable configurations"
        acq_values = self.__call__(configs_for_promotion, only_lowest_fidelity=False)
        next_config = configs_for_promotion[np.argmax(acq_values)]
        self.old_configs_per_fid[int(next_config.fidelity.value)].append(
            next_config.copy()
        )
        next_config.fidelity.value += 1
        return next_config

    def get_config_and_ids(  # pylint: disable=no-self-use
        self,
    ) -> tuple[SearchSpace, str, str | None]:
        if self._num_train_x == 0 and self._initial_design_size >= 1:
            # TODO only at lowest fidelity
            config = self.pipeline_space.sample(
                patience=self.patience, user_priors=True, ignore_fidelity=False
            )
            config.fidelity.value = self.min_fidelity
        elif self.is_init_phase():
            config = self.pipeline_space.sample(
                patience=self.patience, user_priors=True, ignore_fidelity=False
            )
            config.fidelity.value = self.min_fidelity
        elif random.random() < self._random_interleave_prob:
            # TODO only at lowest fidelity
            config = self.pipeline_space.sample(
                patience=self.patience, ignore_fidelity=False, user_priors=False
            )
            config.fidelity.value = self.min_fidelity
        elif len(self._get_promotable_configs(self.train_x_configs)) > 0:
            configs_for_promotion = self._get_promotable_configs(self.train_x_configs)
            config = self._promote_existing(configs_for_promotion)
        else:
            config = self.acquisition_sampler.sample(self.acquisition)

            # if an existing config gets proposed again (which is not uncommon towards the end)
            new_config, _ = self._filter_old_configs([config])
            if len(new_config) == 0:
                config = self.pipeline_space.sample(
                    patience=self.patience, user_priors=False, ignore_fidelity=False
                )
        config_id = str(self._num_train_x + len(self._pending_evaluations) + 1)
        return config.hp_values(), config_id, None
