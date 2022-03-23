import argparse
from argparse import Namespace as NSp
from os import makedirs
from os.path import join

import torch
import wandb
import yaml

import sbibm

from gatsbi.networks.base import WrapGenMultipleSimulations
from gatsbi.optimize import BaseSR as Opt
# from gatsbi.optimize import SequentialOpt as SOpt
from gatsbi.task_utils.benchmarks import (ProposalWrapper, load_generator,
                                          make_generator)
from gatsbi.task_utils.benchmarks.make_results import MakeResults
from gatsbi.task_utils.run_utils import _update_defaults


def main(args):
    args, unknown_args = args

    # Get defaults
    with open(join("tasks", args.task_name, "defaults_sr.yaml"), "r") as f:
        defaults = yaml.load(f, Loader=yaml.Loader)

    # Update defaults
    if len(unknown_args) > 0:
        defaults = _update_defaults(defaults)

    # Make a logger
    print("Making logger")
    makedirs(join("results", args.task_name), exist_ok=True)
    wandb.init(
        project=args.project_name,
        group=args.group_name,
        id=args.run_id,
        resume=args.resume,
        config=defaults,
        notes="",
        dir=join("results", args.task_name)
    )
    config = NSp(**wandb.config)

    run = wandb.run
    with run:
        # Make task and simulator
        print("Making networks")
        task_name = (
            args.task_name if "seq" not in args.task_name else args.task_name[:-4]
        )
        task = sbibm.get_task(task_name)
        simulator = task.get_simulator()
        prior = task.get_prior()

        start = 0
        epochs_per_round = [args.epochs]
        budget_per_round = [args.num_training_simulations]
        seq_impwts = "impwts"

        if "seq" in args.task_name:
            # number of simulations should be equal to budget per round;
            # length of budget list and epoch list = number of rounds
            assert args.num_training_simulations >= sum(config.budget_per_round)
            assert len(config.budget_per_round) == len(config.epochs_per_round)

            start = config.start_with_rnd
            epochs_per_round = config.epochs_per_round
            budget_per_round = config.budget_per_round
            seq_impwts = config.seq_impwts

            if args.resume:
                epochs_per_round[start] = args.epochs

        for rnd, (epochs, budget) in enumerate(
            zip(epochs_per_round[start:], budget_per_round[start:]), start=start
        ):

            # Make proposal, generator and discriminator
            gen = make_generator(
                gen_seed=config.gen_seed,
                seq_impwts=seq_impwts,
                **config.gen_network_kwargs
            )

            dataloader = {}
            classifier_theta = None
            classifier_obs = None

            # Resume from previous state
            if args.resume:
                assert args.resume_dir is not None
                res_chpt = torch.load(
                    join(args.resume_dir, "checkpoint_models%d.pt" % rnd)
                )
                gen = load_generator(res_chpt["generator_state_dict"],
                                     gen,
                                     seq_impwts=config.seq)

                dataloader = torch.load(
                    join(args.resume_dir, "checkpoint_dataloader%d.pt" % rnd)
                )
                print("classifier dataloader")

            # Make proposal prior
            if rnd > 0:
                if args.resume:
                    classifier_theta = torch.load(
                        join(args.resume_dir, "classifier_theta%d.pt" % rnd)
                    )
                    classifier_obs = torch.load(
                        join(args.resume_dir, "classifier_obs%d.pt" % rnd)
                    )
                    directory = args.resume_dir
                else:
                    directory = run.dir
                # Update prior -> proposal
                prop = make_generator(
                    gen_seed=config.gen_seed,
                    seq_impwts=seq_impwts,
                    **config.gen_network_kwargs
                )
                mdl_chpt = torch.load(
                    join(directory, "checkpoint_models%d.pt" % (rnd - 1))
                )
                prop.load_state_dict(mdl_chpt["generator_state_dict"])
                proposal = ProposalWrapper(
                    prop,
                    task.get_observation(config.obs_num),
                    config.seq_impwts,
                    None,
                    config.gen_network_kwargs["add_noise_kwargs"]["lat_dim"],
                ).prior

                prior = proposal

                if not args.resume:
                    # Get dataloader
                    dataloader = torch.load(
                        join(run.dir, "checkpoint_dataloader%d.pt" % (rnd - 1))
                    )
            # Move to GPU
            if not args.no_cuda:
                gen.cuda()

            # wrap the generator to use the SR method
            gen_wrapped = WrapGenMultipleSimulations(gen, n_simulations=args.num_simulations_generator)

            # Make optimiser
            print("Make optimiser")
            batch_size = min(1000, int(config.batch_size_perc * budget))
            lat_dim = config.gen_network_kwargs["add_noise_kwargs"]["lat_dim"]

            if "seq" in args.task_name:
                opt = SOpt(
                    seq_type=config.seq_impwts,
                    classifier_theta=classifier_theta,
                    classifier_obs=classifier_obs,
                    classifier_theta_kwargs=config.classifier_theta_kwargs,
                    classifier_obs_kwargs=config.classifier_obs_kwargs,
                    lat_dim=lat_dim,
                    generator=gen_wrapped,
                    discriminator=dis,
                    prior=prior,
                    simulator=simulator,
                    optim_args=[config.gen_opt_args, config.dis_opt_args],
                    dataloader=dataloader,
                    loss=config.loss,
                    round_number=rnd,
                    reuse_samples=config.reuse_samples,
                    training_opts={
                        "gen_iter": config.gen_iter,
                        "dis_iter": config.dis_iter,
                        "max_norm_gen": config.max_norm_gen,
                        "max_norm_dis": config.max_norm_dis,
                        "num_simulations": budget,
                        "sample_seed": config.sample_seed,
                        "hold_out": config.hold_out,
                        "batch_size": batch_size,
                        "warmup_steps": config.warmup_steps,
                        "num_particles": config.num_particles,
                        "log_dataloader": config.log_dataloader,
                        "stop_thresh": config.stop_thresh,
                    },
                    logger=run,
                )
                print(opt.lat_dist)

            else:
                opt = Opt(
                    gen_wrapped,
                    prior,
                    simulator,
                    [config.gen_opt_args],
                    scoring_rule=config.scoring_rule,
                    training_opts={
                        "num_simulations": args.num_training_simulations,
                        "sample_seed": config.sample_seed,
                        "hold_out": config.hold_out,
                        "batch_size": batch_size,
                        "log_dataloader": True,
                        "stop_thresh": config.stop_thresh,
                    },
                    logger=run,
                )
                setattr(opt, "lat_dist", None)
            if args.resume:
                opt.epoch_ct = res_chpt["epoch"]
                opt.dataloader = dataloader

            # Train model
            print("Training")
            opt.train(epochs, 100)

            make_results = MakeResults(
                generator=gen,
                task=task,
                seq_impwts=seq_impwts,
                lat_dist=opt.lat_dist,
                save_dir=opt.logger.dir,
                cuda = not args.no_cuda
            )
            print("round", rnd)
            if rnd > 0:
                opt.logger.log(make_results.calc_c2st(config.obs_num))
            else:
                opt.logger.log(make_results.calc_c2st_all_obs())
        wandb.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project_name", type=str)
    parser.add_argument("--task_name", type=str)
    parser.add_argument("--group_name", type=str, default=None)
    parser.add_argument("--epochs", type=int, default=20000)
    parser.add_argument("--num_training_simulations", type=int, default=10000)
    parser.add_argument("--num_simulations_generator", type=int, default=3)
    parser.add_argument("--resume", type=bool, default=False)
    parser.add_argument("--run_id", type=str, default=None)
    parser.add_argument("--resume_dir", type=str, default=None)
    parser.add_argument("--no_cuda", action="store_true")
    main(parser.parse_known_args())