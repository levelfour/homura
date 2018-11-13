from abc import ABCMeta, abstractmethod
from typing import Callable, Iterable, Dict

import torch
from torch import nn

from ._vocabulary import *
from .callbacks import CallbackList, Callback
from .optimizer import Optimizer
from .reporter.wrapper import TQDMWrapper
from .scheduler import Scheduler


class TrainerBase(metaclass=ABCMeta):

    def __init__(self, model: nn.Module or Dict[str, nn.Module],
                 optimizer: Optimizer or Dict[str, Optimizer],
                 loss_f: Callable or Dict[str, Callable], *,
                 callbacks: Callback = None,
                 scheduler: Scheduler or Dict[Scheduler] = None,
                 verb=True, use_cudnn_bnenchmark=True, **kwargs):
        """
        :param model: model to be trained
        :param optimizer: optimizer for the model. If dict,  {"name": "optimizer name", **kwargs}.
        :param loss_f: loss function
        :param callbacks: callbacks
        :param scheduler: learning rate scheduler
        :param verb:
        :param use_cudnn_bnenchmark:
        :param kwargs:
        """

        self._device = "cuda" if torch.cuda.is_available() else "cpu"

        # set model(s)
        if isinstance(model, nn.Module):
            self.model = model
            self._is_single_model = True
        elif isinstance(model, dict):
            self.model = nn.ModuleDict(dict)
            self._is_single_model = False
        else:
            raise TypeError(f"Unknown type for arg. model. Expected nn.Module or "
                            f"Dict[str, Module] but got {type(model)}")

        if self._device == "cuda":
            if use_cudnn_bnenchmark:
                torch.backends.cudnn.benchmark = True
            self.model.to(self._device)

        # set optimizer(s)
        if isinstance(optimizer, Optimizer):
            optimizer.set_model(self.model)
            self.optimizer = optimizer.optim
        elif isinstance(optimizer, dict):
            if not isinstance(model, dict):
                raise TypeError(f"model is not dict but optimizer is dict!")
            _opt = {}
            # self.model is nn.ModuleDict
            for k, opt in optimizer.items():
                m = self.model._modules.get(k)
                if m is None:
                    raise KeyError(f"No such key {k} in model!")
                opt.set_model(m)
                _opt[k] = opt.optim
            self.optimizer = _opt

        # set scheduler(s)
        if scheduler is None:
            self._scheduler = None
        elif isinstance(scheduler, Scheduler):
            scheduler.set_optimizer(self.optimizer)
            self._scheduler = scheduler.scheduler
        elif isinstance(scheduler, dict):
            if not isinstance(optimizer, dict):
                raise TypeError(f"optimizer is not dict but scheduler is dict!")
            _schdlr = {}
            for k, schdlr in scheduler.items():
                opt = self.optimizer.get(k)
                if opt is None:
                    raise KeyError(f"No such key {k} in optimizer")
                schdlr.set_optimizer(opt)
                _schdlr[k] = schdlr.scheduler
            self._scheduler = _schdlr

        self.loss_f = loss_f

        # set callback(s)
        if isinstance(callbacks, CallbackList):
            self._callbacks = callbacks
        else:
            self._callbacks = CallbackList(callbacks)

        self._step = 0
        self._epoch = 0
        self._verb = verb
        self._is_train = True

        # set kwargs
        for k, v in kwargs.items():
            if hasattr(self, k):
                raise AttributeError(f"{self} already has {k}")
            setattr(self, k, v)

        self._start_iteration = {}
        self._end_iteration = {}
        self._start_epoch = {}
        self._end_epoch = {}
        self._end_all = {}

    @abstractmethod
    def iteration(self, data: Iterable[torch.Tensor]) -> Iterable[torch.Tensor]:
        """
        iteration part, user can override
        :param data: data used during a iteration
        :param is_train:
        :return: loss, output
        """

    def register_start_iteration(self, name, data):
        self._start_iteration[name] = data

    def register_end_iteration(self, name, data):
        self._end_iteration[name] = data

    def register_start_epoch(self, name, data):
        self._start_epoch[name] = data

    def register_end_epoch(self, name, data):
        self._end_epoch[name] = data

    def register_end_all(self, name, data):
        self._end_all[name] = data

    def _iteration(self, data: Iterable[torch.Tensor], name: str):
        with torch.no_grad():
            _start_iteration = {MODEL: self.model,
                                STEP: self._step,
                                NAME: name,
                                TRAINER: self}
            _start_iteration.update(self._start_iteration)
            self._callbacks.start_iteration(_start_iteration)
        loss, output = self.iteration(data)
        with torch.no_grad():
            _end_iteration = {OUTPUT: output.cpu(),
                              DATA: data,
                              MODEL: self.model,
                              LOSS: loss.data.item(),
                              STEP: self._step,
                              NAME: name,
                              TRAINER: self}
            _end_iteration.update(self._end_iteration)
            self._callbacks.end_iteration(_end_iteration)

    def _loop(self, data_loader, name: str):
        with torch.no_grad():
            _start_epoch = {MODEL: self.model,
                            NAME: name,
                            TRAINER: self}
            _start_epoch.update(self._start_epoch)
            self._callbacks.start_epoch(_start_epoch)

        data_loader = TQDMWrapper(data_loader) if self._verb else data_loader

        for data in data_loader:
            self._iteration(data, name)
            if self.is_train:
                self._step += 1

        with torch.no_grad():
            _end_epoch = {MODEL: self.model,
                          OPTIMIZER: self.optimizer,
                          EPOCH: self._epoch,
                          NAME: name,
                          ITER_PER_EPOCH: len(data_loader),
                          TRAINER: self}
            _end_epoch.update(self._end_epoch)
            self._callbacks.end_epoch(_end_epoch)

    def train(self, data_loader):
        self._is_train = True
        self.model.train()
        with torch.enable_grad():
            self._loop(data_loader, name=TRAIN)
        if self._scheduler is not None:
            self._scheduler.step()
        self._epoch += 1

    def test(self, data_loader, name=TEST):
        self._is_train = False
        self.model.eval()
        with torch.no_grad():
            self._loop(data_loader, name=name)

    def run(self, epochs, train_data, test_data):
        try:
            for ep in range(1, epochs + 1):
                self.train(train_data)
                self.test(test_data)
            self._exit()

        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            self._callbacks.close()

    def _exit(self):
        with torch.no_grad():
            _end_all = {MODEL: self.model,
                        OPTIMIZER: self.optimizer,
                        TRAINER: self}
            _end_all.update(self._end_all)
            self._callbacks.end_all(_end_all)

    @property
    def is_train(self):
        return self._is_train

    def to_device(self, data, **kwargs):
        """
        Handle tuple of data
        :param data:
        :param kwargs:
        :return:
        """
        return (t.to(self._device, **kwargs) for t in data)


class SupervisedTrainer(TrainerBase):
    def __init__(self, model: nn.Module, optimizer: Optimizer, loss_f: Callable, *,
                 callbacks: Callback = None, scheduler: Scheduler = None,
                 verb=True, use_cudnn_bnenchmark=True, **kwargs):
        if isinstance(model, dict):
            raise TypeError(f"{type(self)} does not support dict model")
        super(SupervisedTrainer, self).__init__(model, optimizer, loss_f, callbacks=callbacks, scheduler=scheduler,
                                                verb=verb, use_cudnn_bnenchmark=use_cudnn_bnenchmark, **kwargs)

    def iteration(self, data: Iterable[torch.Tensor]):
        input, target = self.to_device(data)
        output = self.model(input)
        loss = self.loss_f(output, target)
        if self.is_train:
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
        return loss, output


# alias
Trainer = SupervisedTrainer
