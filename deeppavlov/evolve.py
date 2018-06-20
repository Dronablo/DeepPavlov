"""
Copyright 2017 Neural Networks and Deep Learning lab, MIPT

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import argparse
from pathlib import Path
import sys
import os
import json
from copy import deepcopy
from subprocess import Popen, PIPE
import pandas as pd

p = (Path(__file__) / ".." / "..").resolve()
sys.path.append(str(p))

from deeppavlov.core.common.errors import ConfigError
from deeppavlov.models.evolution.evolution_param_generator import ParamsEvolution
from deeppavlov.core.common.file import read_json, save_json
from deeppavlov.core.common.log import get_logger

log = get_logger(__name__)

parser = argparse.ArgumentParser()

parser.add_argument("config_path", help="path to a pipeline json config", type=str)
parser.add_argument('--key_main_model', help='key inserted in dictionary of main model in pipe', default="main")
parser.add_argument('--p_cross', help='probability of crossover', type=float, default=0.2)
parser.add_argument('--pow_cross', help='crossover power', type=float, default=0.1)
parser.add_argument('--p_mut', help='probability of mutation', type=float, default=1.)
parser.add_argument('--pow_mut', help='mutation power', type=float, default=0.1)

parser.add_argument('--p_size', help='population size', type=int, default=10)
parser.add_argument('--gpus', help='visible GPUs divided by comma <<,>>', default="-1")
parser.add_argument('--train_partition',
                    help='partition of splitted train file', default=1)
parser.add_argument('--start_from_population',
                    help='population number to start from. 0 means from scratch', default=0)
parser.add_argument('--path_to_population',
                    help='path to population to start from', default="")
parser.add_argument('--elitism_with_weights',
                    help='whether to save elite models with weights or without', default=0)


def find_config(pipeline_config_path: str):
    if not Path(pipeline_config_path).is_file():
        configs = [c for c in Path(__file__).parent.glob(f'configs/**/{pipeline_config_path}.json')
                   if str(c.with_suffix('')).endswith(pipeline_config_path)]  # a simple way to not allow * and ?
        if configs:
            log.info(f"Interpreting '{pipeline_config_path}' as '{configs[0]}'")
            pipeline_config_path = str(configs[0])
    return pipeline_config_path


def main():
    args = parser.parse_args()

    pipeline_config_path = find_config(args.config_path)
    key_main_model = args.key_main_model
    population_size = args.p_size
    gpus = [int(gpu) for gpu in args.gpus.split(",")]
    train_partition = int(args.train_partition)
    start_from_population = int(args.start_from_population)
    path_to_population = args.path_to_population
    elitism_with_weights = int(args.elitism_with_weights)

    p_crossover = args.p_cross
    pow_crossover = args.pow_cross
    p_mutation = args.p_mut
    pow_mutation = args.pow_mut

    basic_params = read_json(pipeline_config_path)
    log.info("Given basic params: {}\n".format(json.dumps(basic_params, indent=2)))

    evolution = ParamsEvolution(population_size=population_size,
                                p_crossover=p_crossover, crossover_power=pow_crossover,
                                p_mutation=p_mutation, mutation_power=pow_mutation,
                                key_main_model=key_main_model,
                                seed=42,
                                train_partition=train_partition,
                                elitism_with_weights=elitism_with_weights,
                                **basic_params)

    considered_metrics = evolution.get_value_from_config(evolution.basic_config,
                                                         list(evolution.find_model_path(
                                                             evolution.basic_config, "metrics"))[0] + ["metrics"])
    evolve_metric = considered_metrics[0]

    result_file = Path(evolution.get_value_from_config(evolution.basic_config,
                                                       evolution.main_model_path + ["save_path"])
                       ).joinpath("result_table.csv")
    result_table_columns = []
    result_table_dict = {}
    for el in considered_metrics:
        result_table_dict[el + "_valid"] = []
        result_table_dict[el + "_test"] = []
        result_table_columns.extend([el + "_valid", el + "_test"])

    result_table_dict["params"] = []
    result_table_columns.append("params")

    if start_from_population == 0:
        iters = 0
        result_table = pd.DataFrame(result_table_dict)
        result_table.loc[:, result_table_columns].to_csv(result_file, index=False, sep='\t')

        log.info("\nIteration #{} starts\n".format(iters))
        population = evolution.first_generation()
    else:
        iters = start_from_population
        log.info("\nIteration #{} starts\n".format(iters))

        population = []
        for i in range(population_size):
            population.append(read_json(Path(path_to_population).joinpath(
                "model_" + str(i)).joinpath("config.json")))
            population[i] = evolution.insert_value_or_dict_into_config(
                population[i], evolution.main_model_path + ["save_path"],
                str(Path(
                    evolution.get_value_from_config(evolution.basic_config, evolution.main_model_path + ["save_path"])
                    ).joinpath(
                    "population_" + str(start_from_population)).joinpath(
                    "model_" + str(i)).joinpath(
                    "model")))

            population[i] = evolution.insert_value_or_dict_into_config(
                population[i], evolution.main_model_path + ["load_path"],
                str(Path(
                    evolution.get_value_from_config(population[i], evolution.main_model_path + ["load_path"]))))

    run_population(population, evolution, gpus)
    population_scores = results_to_table(population, evolution, considered_metrics,
                                         result_file, result_table_columns)[evolve_metric]
    log.info("Population scores: {}".format(population_scores))
    log.info("\nIteration #{} was done\n".format(iters))
    iters += 1

    while True:
        log.info("\nIteration #{} starts\n".format(iters))
        population = evolution.next_generation(population, population_scores, iters)
        run_population(population, evolution, gpus)
        population_scores = results_to_table(population, evolution, considered_metrics,
                                             result_file, result_table_columns)[evolve_metric]
        log.info("Population scores: {}".format(population_scores))
        log.info("\nIteration #{} was done\n".format(iters))
        iters += 1


def run_population(population, evolution, gpus):
    """
    Change save and load paths for obtained population, save config.json with model config,
    run population via current python executor (with which evolve.py already run)
    and on given devices (-1 means CPU, other integeres - visible for evolve.py GPUs)
    Args:
        population: list of dictionaries - configs of current population
        evolution: ParamsEvolution
        gpus: list of given devices (list of integers)

    Returns:
        None
    """
    population_size = len(population)
    for k in range(population_size // len(gpus) + 1):
        procs = []
        for j in range(len(gpus)):
            i = k * len(gpus) + j
            if i < population_size:
                save_path = Path(evolution.get_value_from_config(population[i],
                                                                 evolution.main_model_path + ["save_path"])).parent

                save_path.mkdir(parents=True, exist_ok=True)
                f_name = save_path.joinpath("config.json")
                save_json(population[i], f_name)

                curr_dir_path = os.path.dirname(os.path.realpath('__file__'))
                if len(gpus) == 1 and gpus[0] == -1:
                    procs.append(Popen("{} {}/deep.py train {}"
                                       " 1>{}/out.txt 2>{}/err.txt".format(sys.executable,
                                                                           curr_dir_path,
                                                                           str(f_name),
                                                                           str(save_path),
                                                                           str(save_path)
                                                                           ),
                                       shell=True, stdout=PIPE, stderr=PIPE))
                else:
                    procs.append(Popen("CUDA_VISIBLE_DEVICES={} {} {}/deep.py train {}"
                                 " 1>{}/out.txt 2>{}/err.txt".format(gpus[j],
                                                                     sys.executable,
                                                                     curr_dir_path,
                                                                     str(f_name),
                                                                     str(save_path),
                                                                     str(save_path)
                                                                     ),
                                       shell=True, stdout=PIPE, stderr=PIPE))
        for j, proc in enumerate(procs):
            i = k * len(gpus) + j
            log.info(f'wait on {i}th proc')
            proc.wait()
    return None


def results_to_table(population, evolution, considered_metrics, result_file, result_table_columns):
    population_size = len(population)
    validate_best = evolution.get_value_from_config(evolution.basic_config,
                                                    list(evolution.find_model_path(
                                                        evolution.basic_config, "validate_best"))[0]
                                                    + ["validate_best"])
    test_best = evolution.get_value_from_config(evolution.basic_config,
                                                list(evolution.find_model_path(
                                                    evolution.basic_config, "test_best"))[0]
                                                + ["test_best"])
    if (not validate_best) and test_best:
        log.info("validate_best is set to False. Tuning parameters on test")
    elif (not validate_best) and (not test_best):
        raise ConfigError("validate_best and test_best are set to False. Can not evolve.")

    population_metrics = {}
    for m in considered_metrics:
        population_metrics[m] = []
    for i in range(population_size):
        with open(str(Path(evolution.get_value_from_config(
                population[i],
                evolution.main_model_path + ["save_path"])).parent.joinpath("out.txt")), "r") as fout:
            reports_data = fout.read().splitlines()[-2:]
        reports = []
        for i in range(2):
            try:
                reports.append(json.loads(reports_data[i]))
            except:
                pass

        val_results = {}
        test_results = {}
        for m in considered_metrics:
            val_results[m] = None
            test_results[m] = None
        if len(reports) == 2 and "valid" in reports[0].keys() and "test" in reports[1].keys():
            val_results = reports[0]["metrics"]
            test_results = reports[1]
        elif len(reports) == 1 and "valid" in reports[0].keys():
            val_results = reports[0]["metrics"]
        elif len(reports) == 1 and "test" in reports[0].keys():
            test_results = reports[0]["metrics"]

        result_table_dict = {}
        for el in result_table_columns:
            result_table_dict[el] = []

        for m in considered_metrics:
            result_table_dict[m + "_valid"].append(val_results[m])
            result_table_dict[m + "_test"].append(test_results[m])
            if validate_best:
                population_metrics[m].append(val_results[m])
            elif test_best:
                population_metrics[m].append(test_results[m])

        result_table_dict[result_table_columns[-1]] = [population[i]]
        result_table = pd.DataFrame(result_table_dict)
        result_table.loc[:, result_table_columns].to_csv(result_file, index=False, sep='\t', mode='a', header=None)

    return population_metrics


if __name__ == "__main__":
    main()