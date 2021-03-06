# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
# pylint: disable=wildcard-import, unused-variable
"""Gluon Estimator"""

import copy
import warnings

from .event_handler import MetricHandler, ValidationHandler, LoggingHandler, StoppingHandler
from .event_handler import TrainBegin, EpochBegin, BatchBegin, BatchEnd, EpochEnd, TrainEnd
from .event_handler import _check_event_handlers
from .utils import _check_metrics, _suggest_metric_for_loss, _check_handler_metric_ref
from ...data import DataLoader
from ...loss import Loss as gluon_loss
from ...trainer import Trainer
from ...utils import split_and_load
from .... import autograd
from ....context import Context, cpu, gpu, num_gpus
from ....metric import Loss as metric_loss

__all__ = ['Estimator']


class Estimator(object):
    """Estimator Class for easy model training

    :py:class:`Estimator` can be used to facilitate the training & validation process


    Parameters
    ----------
    net : gluon.Block
        The model used for training.
    loss : gluon.loss.Loss
        Loss (objective) function to calculate during training.
    metrics : EvalMetric or list of EvalMetric
        Metrics for evaluating models.
    initializer : Initializer
        Initializer to initialize the network.
    trainer : Trainer
        Trainer to apply optimizer on network parameters.
    context : Context or list of Context
        Device(s) to run the training on.
    """

    def __init__(self, net,
                 loss,
                 metrics=None,
                 initializer=None,
                 trainer=None,
                 context=None):

        self.net = net
        self.loss = self._check_loss(loss)
        self._train_metrics = _check_metrics(metrics)
        self._add_default_training_metrics()
        self._add_validation_metrics()

        self.context = self._check_context(context)
        self._initialize(initializer)
        self.trainer = self._check_trainer(trainer)

    def _check_loss(self, loss):
        if not isinstance(loss, gluon_loss):
            raise ValueError("loss must be a Loss, "
                             "refer to gluon.loss.Loss:{}".format(loss))
        return loss

    def _check_context(self, context):
        # infer available context
        gpus = num_gpus()
        available_gpus = [gpu(i) for i in range(gpus)]

        if context:
            # check context values, only accept Context or a list of Context
            if isinstance(context, Context):
                context = [context]
            elif isinstance(context, list) and all([isinstance(c, Context) for c in context]):
                context = context
            else:
                raise ValueError("context must be a Context or a list of Context, "
                                 "for example mx.cpu() or [mx.gpu(0), mx.gpu(1)], "
                                 "refer to mxnet.Context:{}".format(context))
            for ctx in context:
                assert ctx in available_gpus or str(ctx).startswith('cpu'), \
                    "%s is not available, please make sure " \
                    "your context is in one of: mx.cpu(), %s" % \
                    (ctx, ", ".join([str(ctx) for ctx in available_gpus]))
        else:
            # provide default context
            if gpus > 0:
                # only use 1 GPU by default
                if gpus > 1:
                    warnings.warn("You have multiple GPUs, gpu(0) will be used by default."
                                  "To utilize all your GPUs, specify context as a list of gpus, "
                                  "e.g. context=[mx.gpu(0), mx.gpu(1)] ")
                context = [gpu(0)]
            else:
                context = [cpu()]
        return context

    def _initialize(self, initializer):
        # initialize the network
        if not self._is_initialized():
            # net is partially or not initialized,
            # initialize with user specified initializer
            # if initializer is None, default initializer will be used
            # do not re-init layers already initialized
            if initializer:
                self.net.initialize(init=initializer, ctx=self.context)
            else:
                self.net.initialize(ctx=self.context)
        elif initializer:
            # net is fully initialized, and user passed not None initializer
            # do not force reinitialize, give warning
            warnings.warn("Network already fully initialized, skipping initialization. "
                          "You don't need to pass initializer if you already "
                          "initialized your net. "
                          "You can use net.initialize(init=your_initializer, force_reinit=True)"
                          "to force re-initialize.")

    def _check_trainer(self, trainer):
        # handle trainer
        if not trainer:
            warnings.warn("No trainer specified, default SGD optimizer "
                          "with learning rate 0.001 is used.")
            trainer = Trainer(self.net.collect_params(),
                              'sgd', {'learning_rate': 0.001})
        elif not isinstance(trainer, Trainer):
            raise ValueError("Trainer must be a Gluon Trainer instance, refer to "
                             "gluon.Trainer:{}".format(trainer))
        return trainer

    def _is_initialized(self):
        param_dict = self.net.collect_params()
        for param in param_dict:
            try:
                param_dict[param].list_ctx()
            except RuntimeError:
                return False
        return True

    def _get_data_and_label(self, batch, ctx, batch_axis=0):
        data = batch[0]
        label = batch[1]
        data = split_and_load(data, ctx_list=ctx, batch_axis=batch_axis)
        label = split_and_load(label, ctx_list=ctx, batch_axis=batch_axis)
        return data, label

    def _add_default_training_metrics(self):
        if not self._train_metrics:
            suggested_metric = _suggest_metric_for_loss(self.loss)
            if suggested_metric:
                self._train_metrics = [suggested_metric]
            loss_name = self.loss.name.rstrip('1234567890')
            self._train_metrics.append(metric_loss(loss_name))

        for metric in self._train_metrics:
            metric.name = "training " + metric.name

    def _add_validation_metrics(self):
        self._val_metrics = [copy.deepcopy(metric) for metric in self._train_metrics]

        for metric in self._val_metrics:
            metric.name = "validation " + metric.name

    @property
    def train_metrics(self):
        return self._train_metrics

    @property
    def val_metrics(self):
        return self._val_metrics

    def evaluate_batch(self,
                       val_batch,
                       val_metrics,
                       batch_axis=0):
        """Evaluate model on a batch of validation data.

        Parameters
        ----------
        val_batch : tuple
            Data and label of a batch from the validation data loader.
        val_metrics : EvalMetric or list of EvalMetrics
            Metrics to update validation result.
        batch_axis : int, default 0
            Batch axis to split the validation data into devices.
        """
        data, label = self._get_data_and_label(val_batch, self.context, batch_axis)
        pred = [self.net(x) for x in data]
        loss = [self.loss(y_hat, y) for y_hat, y in zip(pred, label)]
        # update metrics
        for metric in val_metrics:
            if isinstance(metric, metric_loss):
                metric.update(0, loss)
            else:
                metric.update(label, pred)

    def evaluate(self,
                 val_data,
                 val_metrics,
                 batch_axis=0):
        """Evaluate model on validation data.

        This function calls :py:func:`evaluate_batch` on each of the batches from the
        validation data loader. Thus, for custom use cases, it's possible to inherit the
        estimator class and override :py:func:`evaluate_batch`.

        Parameters
        ----------
        val_data : DataLoader
            Validation data loader with data and labels.
        val_metrics : EvalMetric or list of EvalMetrics
            Metrics to update validation result.
        batch_axis : int, default 0
            Batch axis to split the validation data into devices.
        """
        if not isinstance(val_data, DataLoader):
            raise ValueError("Estimator only support input as Gluon DataLoader. Alternatively, you "
                             "can transform your DataIter or any NDArray into Gluon DataLoader. "
                             "Refer to gluon.data.DataLoader")

        for metric in val_metrics:
            metric.reset()

        for _, batch in enumerate(val_data):
            self.evaluate_batch(batch, val_metrics, batch_axis)

    def fit_batch(self, train_batch,
                  batch_axis=0):
        """Trains the model on a batch of training data.

        Parameters
        ----------
        train_batch : tuple
            Data and label of a batch from the training data loader.
        batch_axis : int, default 0
            Batch axis to split the training data into devices.

        Returns
        -------
        data: List of NDArray
            Sharded data from the batch.
        label: List of NDArray
            Sharded label from the batch.
        pred: List of NDArray
            Prediction of each of the shareded batch.
        loss: List of NDArray
            Loss of each of the shareded batch.
        """
        data, label = self._get_data_and_label(train_batch, self.context, batch_axis)

        batch_size = train_batch[0].shape[batch_axis]

        with autograd.record():
            pred = [self.net(x) for x in data]
            loss = [self.loss(y_hat, y) for y_hat, y in zip(pred, label)]

        for l in loss:
            l.backward()

        self.trainer.step(batch_size)

        return data, label, pred, loss

    def fit(self, train_data,
            val_data=None,
            epochs=None,
            event_handlers=None,
            batches=None,
            batch_axis=0):
        """Trains the model with a given :py:class:`DataLoader` for a specified
        number of epochs or batches. The batch size is inferred from the
        data loader's batch_size.

        This function calls :py:func:`fit_batch` on each of the batches from the
        training data loader. Thus, for custom use cases, it's possible to inherit the
        estimator class and override :py:func:`fit_batch`.

        Parameters
        ----------
        train_data : DataLoader
            Training data loader with data and labels.
        val_data : DataLoader, default None
            Validation data loader with data and labels.
        epochs : int, default None
            Number of epochs to iterate on the training data.
            You can only specify one and only one type of iteration(epochs or batches).
        event_handlers : EventHandler or list of EventHandler
            List of :py:class:`EventHandlers` to apply during training.
        batches : int, default None
            Number of batches to iterate on the training data.
            You can only specify one and only one type of iteration(epochs or batches).
        batch_axis : int, default 0
            Batch axis to split the training data into devices.
        """
        if not isinstance(train_data, DataLoader):
            raise ValueError("Estimator only support input as Gluon DataLoader. Alternatively, you "
                             "can transform your DataIter or any NDArray into Gluon DataLoader. "
                             "Refer to gluon.data.dataloader")

        # must specify one and only one of epochs or batches
        if (not epochs) == (not batches):
            raise ValueError(
                "Fit only support exactly one type of iteration, "
                "train by number of epochs or number of batches."
                "Please specify one and only one of: epochs or batches.")

        self.max_epoch = epochs
        self.max_batch = batches

        # provide default handlers
        event_handlers = self._prepare_default_handlers(val_data, event_handlers)

        train_begin, epoch_begin, batch_begin, \
        batch_end, epoch_end, train_end = self._categorize_handlers(event_handlers)

        # pass a reference to all event handlers
        estimator_ref = self
        # training begin
        for handler in train_begin:
            handler.train_begin(estimator_ref)

        while True:
            # epoch begin
            for handler in epoch_begin:
                handler.epoch_begin(estimator_ref)

            for i, batch in enumerate(train_data):
                # batch begin
                for handler in batch_begin:
                    handler.batch_begin(estimator_ref, batch=batch)

                _, label, pred, loss = self.fit_batch(batch, batch_axis)

                # batch end

                batch_end_result = []
                for handler in batch_end:
                    batch_end_result.append(handler.batch_end(estimator_ref, batch=batch,
                                                              pred=pred, label=label, loss=loss))
                # if any handler signaled to stop
                if any(batch_end_result):
                    break

            # epoch end
            epoch_end_result = []
            for handler in epoch_end:
                epoch_end_result.append(handler.epoch_end(estimator_ref))
            # if any handler signaled to stop
            if any(epoch_end_result):
                break

        # train end
        for handler in train_end:
            handler.train_end(estimator_ref)

    def _prepare_default_handlers(self, val_data, event_handlers):
        event_handlers = _check_event_handlers(event_handlers)
        added_default_handlers = []

        # no need to add to default handler check as StoppingHandler does not use metrics
        added_default_handlers.append(StoppingHandler(self.max_epoch, self.max_batch))

        if not any(isinstance(handler, MetricHandler) for handler in event_handlers):
            added_default_handlers.append(MetricHandler(train_metrics=self.train_metrics))

        if not any(isinstance(handler, ValidationHandler) for handler in event_handlers):
            # no validation handler
            if val_data:
                val_metrics = self.val_metrics
                # add default validation handler if validation data found
                added_default_handlers.append(ValidationHandler(val_data=val_data,
                                                                eval_fn=self.evaluate,
                                                                val_metrics=val_metrics))
            else:
                # set validation metrics to None if no validation data and no validation handler
                val_metrics = []

        if not any(isinstance(handler, LoggingHandler) for handler in event_handlers):
            added_default_handlers.append(LoggingHandler(train_metrics=self.train_metrics,
                                                         val_metrics=val_metrics))

        # if there is a mix of user defined event handlers and default event handlers
        # they should have the same set of metrics
        mixing_handlers = event_handlers and added_default_handlers

        event_handlers.extend(added_default_handlers)

        if mixing_handlers:
            msg = "The following default event handlers are added: {}.".format(
                ", ".join([type(h).__name__ for h in added_default_handlers]))
            warnings.warn(msg)


            # check if all handlers have the same set of references to metrics
            known_metrics = set(self.train_metrics + self.val_metrics)
            for handler in event_handlers:
                _check_handler_metric_ref(handler, known_metrics)

        event_handlers.sort(key=lambda handler: getattr(handler, 'priority', 0))
        return event_handlers

    def _categorize_handlers(self, event_handlers):
        """
        categorize handlers into 6 event lists to avoid calling empty methods
        for example, only event handlers with train_begin method
        implemented will be called at train begin
        """

        train_begin = []
        epoch_begin = []
        batch_begin = []
        batch_end = []
        epoch_end = []
        train_end = []
        for handler in event_handlers:
            if isinstance(handler, TrainBegin):
                train_begin.append(handler)
            if isinstance(handler, EpochBegin):
                epoch_begin.append(handler)
            if isinstance(handler, BatchBegin):
                batch_begin.append(handler)
            if isinstance(handler, BatchEnd):
                batch_end.append(handler)
            if isinstance(handler, EpochEnd):
                epoch_end.append(handler)
            if isinstance(handler, TrainEnd):
                train_end.append(handler)
        return train_begin, epoch_begin, batch_begin, batch_end, epoch_end, train_end
