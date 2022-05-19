
import os
import sys
import ray
import torch
import inspect
import importlib
import numpy as np
from torch.nn.modules.loss import CrossEntropyLoss

from args import parse_arguments
current_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)
from tasks.cifar10.cct import Net
from simulators.clientbuilder import ClientBuilder
from simulators.server import TorchServer
from settings.cifar10 import cifar10
from utils import top1_accuracy, initialize_logger

options = parse_arguments()
if options.use_actor:
    from simulators.simulator import (ParallelTrainer, DistributedEvaluator)
else:
    from simulators.simulator_trainer import (ParallelTrainer, DistributedEvaluator)

agg_path = importlib.import_module('aggregators.%s' % options.agg)
agg_scheme = getattr(agg_path, options.agg.capitalize())


def main(args):
    initialize_logger(options.log_dir)
    device = torch.device("cuda" if args.use_cuda else "cpu")
    
    kwargs = {"pin_memory": True} if args.use_cuda else {}
    
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    opt = importlib.import_module(options.model_path)
    Model = getattr(opt, "Net")
    model = Model().to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=options.lr)
    loss_func = CrossEntropyLoss().to(device)
    
    metrics = {"top1": top1_accuracy}
    
    server_opt = torch.optim.SGD(model.parameters(), lr=options.lr)
    server = TorchServer(server_opt, model=model)
    
    trainer = ParallelTrainer(
        server=server,
        aggregator=agg_scheme(options),
        pre_batch_hooks=[],
        post_batch_hooks=[],
        max_batches_per_epoch=options.local_round,
        log_interval=args.log_interval,
        metrics=metrics,
        use_cuda=args.use_cuda,
        debug=False,
    )
    
    test_loader = cifar10(
        data_dir=options.data_dir,
        data_path=options.data_path,
        train=False,
        download=True,
        batch_size=options.test_batch_size,
        shuffle=False,
        worker_rank=None,
        **kwargs,
    )
    
    scheduler = torch.optim.lr_scheduler.MultiStepLR(
        server_opt, milestones=[75, 100], gamma=0.5
    )
    
    evaluator = DistributedEvaluator(
        model=model,
        data_loader=test_loader,
        loss_func=loss_func,
        device=device,
        metrics=metrics,
        use_cuda=args.use_cuda,
        debug=False,
    )
    
    client_builder = ClientBuilder(options=options)
    for worker_rank in range(options.num_clients):
        worker = client_builder.initialize_client(
            trainer=trainer,
            worker_rank=worker_rank,
            model=model,
            optimizer=optimizer,
            loss_func=loss_func,
            device=device,
            use_actor=args.use_actor,
            is_fedavg=args.fedavg,
            kwargs={},
        )
        trainer.add_client(worker)
    
    if args.use_actor:
        trainer.parallel_call(lambda worker: worker.detach_model.remote())
    else:
        trainer.parallel_call(lambda worker: worker.detach_model())
    
    for epoch in range(1, options.round + 1):
        if args.fedavg:
            trainer.train_fedavg(epoch)
        else:
            trainer.train(epoch)
        evaluator.evaluate(epoch)
        scheduler.step()
        print(f"E={epoch}; Learning rate = {scheduler.get_last_lr()[0]:}")


if __name__ == "__main__":
    if not ray.is_initialized():
        # ray.init(local_mode=True, include_dashboard=True, num_gpus=options.num_gpus)
        ray.init(include_dashboard=True, num_gpus=options.num_gpus)
    main(options)
