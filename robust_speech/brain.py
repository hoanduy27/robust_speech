
import os
import sys
import time
import logging
from typing import List
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
from speechbrain.dataio.dataloader import LoopedLoader
import speechbrain as sb
from hyperpyyaml import load_hyperpyyaml
from speechbrain.utils.distributed import run_on_main
from advertorch.attacks import L2PGDAttack
from robust_speech.adversarial.attacks.pgd import ASRL2PGDAttack
from robust_speech.adversarial.metrics import snr, wer, cer
from robust_speech.utils import make_batch_from_waveform, transcribe_batch, load_audio
import robust_speech as rs

logger = logging.getLogger(__name__)

# Define training procedure
class ASRBrain(sb.Brain):
    """
    Intermediate abstract class that specifies some methods for ASR models that can be attacked.
    """

    def compute_forward(self, batch, stage):
        """Forward pass, to be overridden by sub-classes.

        Arguments
        ---------
        batch : torch.Tensor or tensors
            An element from the dataloader, including inputs for processing.
        stage : Union[sb.Stage, rs.Stage]
            The stage of the experiment: sb.Stage.TRAIN, sb.Stage.VALID, sb.Stage.TEST, rs.Stage.ATTACK

        Returns
        -------
        torch.Tensor or Tensors
            The outputs after all processing is complete.
            Directly passed to ``compute_objectives()``.
            In VALID or TEST stage, this should contain the predicted tokens.
            In ATTACK stage, batch.sig should be in the computation graph (no device change, no .detach())
        """
        raise NotImplementedError

    def compute_objectives(self, predictions, batch, stage, adv=False):
        """Compute loss, to be overridden by sub-classes.

        Arguments
        ---------
        predictions : torch.Tensor or Tensors
            The output tensor or tensors to evaluate.
            Comes directly from ``compute_forward()``.
        batch : torch.Tensor or tensors
            An element from the dataloader, including targets for comparison.
        stage : Union[sb.Stage, rs.Stage]
            The stage of the experiment: sb.Stage.TRAIN, sb.Stage.VALID, sb.Stage.TEST, rs.Stage.ATTACK

        Returns
        -------
        loss : torch.Tensor
            A tensor with the computed loss.
        """
        raise NotImplementedError

    def module_train(self):
        self.modules.train()

    def module_eval(self):
        self.modules.eval()

    def get_tokens(self,predictions):
        return predictions[-1]

class PredictionEnsemble:
    def __init__(self,predictions):
        self.predictions=predictions
    def __iter__(self,i):
        return self.predictions[i]
    def __len__(self):
        return len(self.predictions)

class EnsembleASRBrain(ASRBrain):
    def __init__(self,asr_brains, ref_tokens = 0):
        self.nmodels=len(asr_brains)
        self.asr_brains=asr_brains
        self.ref_tokens=ref_tokens # use this model to return tokens
    def compute_forward(self, batch, stage, model_idx=None):
        # concatenate predictions 
        if model_idx is not None:
            return self.asr_brains[model_idx].compute_forward(batch, stage)
        predictions = [] 
        for ab in self.asr_brains:
            pred = ab.compute_forward(batch, stage)
            predictions.append(pred) 
        return PredictionEnsemble(predictions)

    def get_tokens(self,predictions, all=False, model_idx=None): 
        # all or ref
        if isinstance(predictions,PredictionEnsemble):
            assert len(predictions)==self.nmodels
            if all:
                return [self.asr_brains[i].get_tokens(pred) for i,pred in enumerate(predictions)]
            if model_idx is not None:
                return self.asr_brains[model_idx].get_tokens(predictions[model_idx])
            return self.asr_brains[self.ref_tokens].get_tokens(predictions[self.ref_tokens])
        return self.asr_brains[self.ref_tokens].get_tokens(predictions)

    def compute_objectives(self,predictions, batch, stage, average=True, model_idx=None):
        # concatenate of average objectives
        if isinstance(predictions,PredictionEnsemble) or model_idx is None: # many predictions
            assert len(predictions)==self.nmodels
            losses = []
            for i in range(self.nmodels):
                ab = self.asr_brains[i] if model_idx is None else self.asr_brains[model_idx] # one pred per model or n pred per model
                pred = predictions[i] if isinstance(predictions,PredictionEnsemble) else predictions
                loss = ab.compute_objectives(pred, batch, stage)
                losses.append(loss)
            losses = torch.stack(losses,dim=0)
            if average:
                return torch.mean(loss,dim=0)
            return losses
        return self.asr_brains[model_idx].compute_objectives(predictions, batch, stage)

    
class AdvASRBrain(ASRBrain):
    """
    Intermediate abstract class that specifies some methods for ASR models that can be trained adversarially.
    """
    def __init__(  # noqa: C901
        self,
        modules=None,
        opt_class=None,
        hparams=None,
        run_opts=None,
        checkpointer=None,
        attack_class=None
    ):
        ASRBrain.__init__(
            self,
            modules=modules,
            opt_class=opt_class,
            hparams=hparams,
            run_opts=run_opts,
            checkpointer=checkpointer
        )

        self.init_attacker(
            modules=modules,
            opt_class=opt_class,
            hparams=hparams,
            run_opts=run_opts,
            attack_class=attack_class
        )

    def __setattr__(self,name,value, attacker_brain=True):
        if hasattr(self,"attacker") and name != "attacker" and attacker_brain:
            super(AdvASRBrain,self.attacker.asr_brain).__setattr__(name,value)
        super(AdvASRBrain,self).__setattr__(name,value)

    def init_attacker(
        self, 
        modules=None,
        opt_class=None,
        hparams=None,
        run_opts=None,
        attack_class=None
    ):
        if attack_class is not None:
            brain_to_attack = type(self)(
                modules=modules,
                opt_class=opt_class,
                hparams=hparams,
                run_opts=run_opts,
                checkpointer=None,
                attack_class=None
            )
            self.attacker = attack_class(brain_to_attack)

    def compute_forward_adversarial(self, batch, stage):
        assert stage != rs.Stage.ATTACK
        wavs = batch.sig[0]
        if self.attacker is not None:
            adv_wavs = self.attacker.perturb(batch)
            batch.sig = adv_wavs, batch.sig[1]
        res = self.compute_forward(batch,stage)
        batch.sig = wavs, batch.sig[1]
        del adv_wavs
        return res
    

    def fit_batch_adversarial(self, batch):
        """Fit one batch, override to do multiple updates.

        The default implementation depends on a few methods being defined
        with a particular behavior:

        * ``compute_forward()``
        * ``compute_objectives()``

        Also depends on having optimizers passed at initialization.

        Arguments
        ---------
        batch : list of torch.Tensors
            Batch of data to use for training. Default implementation assumes
            this batch has two elements: inputs and targets.

        Returns
        -------
        detached loss
        """
        # Managing automatic mixed precision
        if self.auto_mix_prec:
            self.optimizer.zero_grad()
            with torch.cuda.amp.autocast():
                outputs = self.compute_forward_adversarial(batch, sb.Stage.TRAIN)
                loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)
            self.scaler.scale(loss).backward()
            self.scaler.unscale_(self.optimizer)
            if self.check_gradients(loss):
                self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            outputs = self.compute_forward_adversarial(batch, sb.Stage.TRAIN)
            loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)
            loss.backward()
            if self.check_gradients(loss):
                self.optimizer.step()
            self.optimizer.zero_grad()

        return loss.detach().cpu()

    def evaluate_batch_adversarial(self, batch, stage):
        """Evaluate one batch, override for different procedure than train.

        The default implementation depends on two methods being defined
        with a particular behavior:

        * ``compute_forward()``
        * ``compute_objectives()``

        Arguments
        ---------
        batch : list of torch.Tensors
            Batch of data to use for evaluation. Default implementation assumes
            this batch has two elements: inputs and targets.
        stage : Stage
            The stage of the experiment: Stage.VALID, Stage.TEST

        Returns
        -------
        detached loss
        """

        out = self.compute_forward_adversarial(batch, stage=stage)
        loss = self.compute_objectives(out, batch, stage=stage, adv=True)
        return loss.detach().cpu()

    def fit(
        self,
        epoch_counter,
        train_set,
        valid_set=None,
        progressbar=None,
        train_loader_kwargs={},
        valid_loader_kwargs={},
    ):
        """Iterate epochs and datasets to improve objective.

        Relies on the existence of multiple functions that can (or should) be
        overridden. The following methods are used and expected to have a
        certain behavior:

        * ``fit_batch()``
        * ``evaluate_batch()``
        * ``fit_batch_adversarial()``
        * ``evaluate_batch_adversarial()``
        * ``update_average()``

        If the initialization was done with distributed_count > 0 and the
        distributed_backend is ddp, this will generally handle multiprocess
        logic, like splitting the training data into subsets for each device and
        only saving a checkpoint on the main process.

        Arguments
        ---------
        epoch_counter : iterable
            Each call should return an integer indicating the epoch count.
        train_set : Dataset, DataLoader
            A set of data to use for training. If a Dataset is given, a
            DataLoader is automatically created. If a DataLoader is given, it is
            used directly.
        valid_set : Dataset, DataLoader
            A set of data to use for validation. If a Dataset is given, a
            DataLoader is automatically created. If a DataLoader is given, it is
            used directly.
        train_loader_kwargs : dict
            Kwargs passed to `make_dataloader()` for making the train_loader
            (if train_set is a Dataset, not DataLoader).
            E.G. batch_size, num_workers.
            DataLoader kwargs are all valid.
        valid_loader_kwargs : dict
            Kwargs passed to `make_dataloader()` for making the valid_loader
            (if valid_set is a Dataset, not DataLoader).
            E.g., batch_size, num_workers.
            DataLoader kwargs are all valid.
        progressbar : bool
            Whether to display the progress of each epoch in a progressbar.
        """

        if not (
            isinstance(train_set, DataLoader)
            or isinstance(train_set, LoopedLoader)
        ):
            train_set = self.make_dataloader(
                train_set, stage=sb.Stage.TRAIN, **train_loader_kwargs
            )
        if valid_set is not None and not (
            isinstance(valid_set, DataLoader)
            or isinstance(valid_set, LoopedLoader)
        ):
            valid_set = self.make_dataloader(
                valid_set,
                stage=sb.Stage.VALID,
                ckpt_prefix=None,
                **valid_loader_kwargs,
            )

        self.on_fit_start()

        if progressbar is None:
            progressbar = not self.noprogressbar

        # Iterate epochs
        for epoch in epoch_counter:
            # Training stage
            self.on_stage_start(sb.Stage.TRAIN, epoch)
            self.modules.train()

            # Reset nonfinite count to 0 each epoch
            self.nonfinite_count = 0

            if self.train_sampler is not None and hasattr(
                self.train_sampler, "set_epoch"
            ):
                self.train_sampler.set_epoch(epoch)

            # Time since last intra-epoch checkpoint
            last_ckpt_time = time.time()

            # Only show progressbar if requested and main_process
            enable = progressbar and sb.utils.distributed.if_main_process()
            with tqdm(
                train_set,
                initial=self.step,
                dynamic_ncols=True,
                disable=not enable,
            ) as t:
                for batch in t:
                    self.step += 1
                    loss = self.fit_batch_adversarial(batch)
                    self.avg_train_loss = self.update_average(
                        loss, self.avg_train_loss
                    )
                    t.set_postfix(adv_train_loss=self.avg_train_loss)

                    # Debug mode only runs a few batches
                    if self.debug and self.step == self.debug_batches:
                        break

                    if (
                        self.checkpointer is not None
                        and self.ckpt_interval_minutes > 0
                        and time.time() - last_ckpt_time
                        >= self.ckpt_interval_minutes * 60.0
                    ):
                        # This should not use run_on_main, because that
                        # includes a DDP barrier. That eventually leads to a
                        # crash when the processes'
                        # time.time() - last_ckpt_time differ and some
                        # processes enter this block while others don't,
                        # missing the barrier.
                        if sb.utils.distributed.if_main_process():
                            self._save_intra_epoch_ckpt()
                        last_ckpt_time = time.time()

            # Run train "on_stage_end" on all processes
            self.on_stage_end(sb.Stage.TRAIN, self.avg_train_loss, epoch)
            self.avg_train_loss = 0.0
            self.step = 0

            # Validation stage
            if valid_set is not None:
                self.on_stage_start(sb.Stage.VALID, epoch)
                self.modules.eval()
                avg_valid_loss = 0.0
                avg_valid_adv_loss = 0.0
                for batch in tqdm(
                    valid_set, dynamic_ncols=True, disable=not enable
                ):
                    self.step += 1
                    loss = self.evaluate_batch(batch, stage=sb.Stage.VALID)
                    avg_valid_loss = self.update_average(
                        loss, avg_valid_loss
                    )

                    adv_loss = self.evaluate_batch_adversarial(batch, stage=sb.Stage.VALID)
                    avg_valid_adv_loss = self.update_average(
                        adv_loss, avg_valid_adv_loss
                    )

                    # Debug mode only runs a few batches
                    if self.debug and self.step == self.debug_batches:
                        break

                # Only run validation "on_stage_end" on main process
                self.step = 0
                run_on_main(
                    self.on_stage_end,
                    args=[sb.Stage.VALID, avg_valid_loss, epoch],
                    kwargs={"stage_adv_loss":avg_valid_adv_loss}
                )

            # Debug mode only runs a few epochs
            if (
                self.debug
                and epoch == self.debug_epochs
            ):
                break

    def evaluate(
        self,
        test_set,
        max_key=None,
        min_key=None,
        progressbar=None,
        test_loader_kwargs={},
    ):
        """Iterate test_set and evaluate brain performance. By default, loads
        the best-performing checkpoint (as recorded using the checkpointer).

        Arguments
        ---------
        test_set : Dataset, DataLoader
            If a DataLoader is given, it is iterated directly. Otherwise passed
            to ``self.make_dataloader()``.
        max_key : str
            Key to use for finding best checkpoint, passed to
            ``on_evaluate_start()``.
        min_key : str
            Key to use for finding best checkpoint, passed to
            ``on_evaluate_start()``.
        progressbar : bool
            Whether to display the progress in a progressbar.
        test_loader_kwargs : dict
            Kwargs passed to ``make_dataloader()`` if ``test_set`` is not a
            DataLoader. NOTE: ``loader_kwargs["ckpt_prefix"]`` gets
            automatically overwritten to ``None`` (so that the test DataLoader
            is not added to the checkpointer).

        Returns
        -------
        average test loss
        """
        if progressbar is None:
            progressbar = not self.noprogressbar

        if not (
            isinstance(test_set, DataLoader)
            or isinstance(test_set, LoopedLoader)
        ):
            test_loader_kwargs["ckpt_prefix"] = None
            test_set = self.make_dataloader(
                test_set, sb.Stage.TEST, **test_loader_kwargs
            )
        self.on_evaluate_start(max_key=max_key, min_key=min_key)
        self.on_stage_start(sb.Stage.TEST, epoch=None)
        self.modules.eval()
        avg_test_loss = 0.0
        avg_test_adv_loss = 0.0

        for batch in tqdm(
            test_set, dynamic_ncols=True, disable=not progressbar
        ):
            self.step += 1
            loss = self.evaluate_batch(batch, stage=sb.Stage.TEST)
            avg_test_loss = self.update_average(loss, avg_test_loss)

            adv_loss = self.evaluate_batch_adversarial(batch, stage=sb.Stage.TEST)
            avg_test_adv_loss = self.update_average(adv_loss, avg_test_adv_loss)

            # Debug mode only runs a few batches
            if self.debug and self.step == self.debug_batches:
                break

            # Only run evaluation "on_stage_end" on main process
        run_on_main(
            self.on_stage_end, args=[sb.Stage.TEST, avg_test_loss, None],
                kwargs={"stage_adv_loss":avg_test_adv_loss}
        )
        self.step = 0
        return avg_test_loss