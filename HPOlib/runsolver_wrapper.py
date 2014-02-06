##
# wrapping: A program making it easy to use hyperparameter
# optimization software.
# Copyright (C) 2013 Katharina Eggensperger and Matthias Feurer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from collections import OrderedDict
import imp
import numpy as np
import os
import re
import subprocess
import sys
import time

import HPOlib.Experiment as Experiment
import HPOlib.wrapping_util as wrapping_util

__authors__ = ["Katharina Eggensperger", "Matthias Feurer"]
__contact__ = "automl.org"


# TODO: This should be in a util function sometime in the future
#       Is duplicated in cv.py
def get_optimizer():
    return "_".join(os.getcwd().split("/")[-1].split("_")[0:-2])


def remove_param_metadata(params):
    """
    Check whether some params are defined on the Log scale or with a Q value,
    must be marked with "LOG$_{paramname}" or Q[0-999]_$paramname
    LOG/Q will be removed from the paramname
    """
    for para in params:
        new_name = para
        if "LOG10_" in para:
            pos = para.find("LOG10")
            new_name = para[0:pos] + para[pos+6:]
            # new_name = new_name.strip("_")
            params[new_name] = np.power(10, float(params[para]))
            del params[para]
        elif "LOG2" in para:
            pos = para.find("LOG2_")
            new_name = para[0:pos] + para[pos+5:]
            # new_name = new_name.strip("_")
            params[new_name] = np.power(2, float(params[para]))
            del params[para]
        elif "LOG_" in para:
            pos = para.find("LOG")
            new_name = para[0:pos] + para[pos+4:]
            # new_name = new_name.strip("_")
            params[new_name] = np.exp(float(params[para]))
            del params[para]
        #Check for Q value, returns round(x/q)*q
        m = re.search(r'Q[0-999\.]{1,10}_', para)
        if m is not None:
            pos = new_name.find(m.group(0))
            tmp = new_name[0:pos] + new_name[pos+len(m.group(0)):]
            #tmp = tmp.strip("_")
            q = float(m.group(0)[1:-1])
            params[tmp] = round(float(params[new_name])/q)*q
            del params[new_name]


def load_experiment_file():
    optimizer = get_optimizer()
    experiment = Experiment.Experiment(".", optimizer)
    return experiment


def read_runsolver_output(runsolver_output_file):
    """
    Read the runsolver output, watch out for
    Mem limit exceeded: sending SIGTERM then SIGKILL
    Maximum CPU time exceeded: sending SIGTERM then SIGKILL
    Maximum wall clock time exceeded: sending SIGTERM then SIGKILL
    Maximum VSize exceeded: sending SIGTERM then SIGKILL
    In case one of these happened, send back the worst possible result
    as specified in the config
    """
    limit_exceeded = None
    cpu_time = None
    wallclock_time = None
    solver_ended_section = False
    with open(runsolver_output_file, 'r') as f:
        runsolver_output_content = f.readlines()
        for line in runsolver_output_content:
            if "Maximum CPU time exceeded" in line:
                limit_exceeded = "CPU time exceeded"
            if "Maximum wall clock time exceeded" in line:
                limit_exceeded = "Wall clock time exceeded"
            if "Maximum VSize exceeded" in line:
                limit_exceeded = "VSize exceeded"
            if "Mem limit exceeded" in line:
                limit_exceeded = "Memory exceeded"
            if "Solver just ended. Dumping a history of the last" in line:
                solver_ended_section = True
            if "Real time (s): " in line and solver_ended_section:
                wallclock_time = float(line.split()[3])
            if "CPU time (s): " in line and solver_ended_section:
                cpu_time = float(line.split()[3])
    return cpu_time, wallclock_time, limit_exceeded


def read_run_instance_output(run_instance_output):
    """
    Read the run_instance output file
    """
    result_string = None
    result_array = None
    fh = open(run_instance_output, "r")
    run_instance_content = fh.readlines()
    fh.close()
    result_string = None
    for line in run_instance_content:
        match = re.search(r"\s*[Rr]esult\s+(?:([Ff]or)|([oO]f))\s"
                         r"+(?:(HAL)|(ParamILS)|(SMAC)|([tT]his [wW]rapper))",
                         line)
        if match:
            pos = match.start(0)
            result_string = line[pos:].strip()
            result_array = result_string.split()
            result_array = [value.strip(",") for value in result_array]
            break

    return result_array, result_string


def get_trial_index(experiment, fold, params):
    # Check whether we are in a new configuration; This has to check whether
    # the params were already inserted but also whether the fold already run
    # This is checked twice; the instance_result has to be not NaN and the
    # entry in instance_order has to exist
    new = True
    trial_index = np.NaN
    for idx, trial in enumerate(experiment.trials):
        exp = trial['params']
        if exp == params and (idx, fold) not in experiment.instance_order and \
                (experiment.get_trial_from_id(idx)['instance_results'][fold] == np.NaN or \
                 experiment.get_trial_from_id(idx)['instance_results'][fold] !=
                 experiment.get_trial_from_id(idx)['instance_results'][fold]):
            new = False
            trial_index = idx
            break
    if new:
        trial_index = experiment.add_job(params)
    return trial_index


def parse_command_line():
    # Parse options and arguments
    usage = "This script pickles the params and runs the runsolver with " + \
            "run_instance and extract the output for the optimizer \n" + \
            "The output is printed im a SMACish way: \n\n" + \
            "'Result for ParamILS: <solved>, <runtime>, <runlength>, " + \
            "<quality>, <seed>, <additional rundata>' \n\n" + \
            "Usage: runsolver_wrapper <instancename> " + \
            "<instancespecificinformation> <cutofftime> <cutofflength> " + \
            "<seed> <param> <param> <param>\n" + \
            "<instancename> might be the optimizer name if not" + \
            " called by smac\n"
    if len(sys.argv) < 7:
        sys.stdout.write(usage)
        exit(1)

    # Then get some information for run_instance
    fold = int(sys.argv[1])
    seed = int(sys.argv[5])
    return fold, seed


def get_function_filename(cfg):
    if os.path.isabs(cfg.get("DEFAULT", "function")):
        fn_path = cfg.get("DEFAULT", "function")
    else:
        fn_path = cfg.get("DEFAULT", "function")
        fn_path_parent = os.path.join("..", cfg.get("DEFAULT", "function"))
    fn_name, ext = os.path.splitext(os.path.basename(fn_path))
    try:
        fn = imp.load_source(fn_name, fn_path)
    except (ImportError, IOError) as e:
        print "Raised", e, "trying to recover..."
        try:
            print os.path.exists(fn_path_parent)
            fn = imp.load_source(fn_name, fn_path_parent)
        except (ImportError, IOError):
            print os.path.join(fn_path_parent)
            print(("Could not find\n%s\n\tin\n%s\n\tor its parent directory " +
                   "relative to\n%s")
                  % (fn_name, fn_path, os.getcwd()))
            import traceback

            print traceback.format_exc()
            sys.exit(1)
    fn_filename = os.path.realpath(fn.__file__)
    if fn_filename[-3:] == "pyc":
        fn_filename = fn_filename[:-1]

    return fn_filename


def make_command(cfg, fold, param_string, run_instance_output):
    time_limit = cfg.getint('DEFAULT', 'runsolver_time_limit')
    memory_limit = cfg.getint('DEFAULT', 'memory_limit')
    cpu_limit = cfg.getint('DEFAULT', 'cpu_limit')
    fn_filename = get_function_filename(cfg)
    python_cmd = cfg.get("DEFAULT", "leading_algo_info") + " python " + \
                 fn_filename + " --fold %d --folds %d --params %s" % \
                 (fold, cfg.getint("DEFAULT", "numberCV"), param_string)
    # Do not write the actual task in quotes because runsolver will not work
    # then; also we need use-pty antd timestamp so that the "solver" output
    # is flushed to the output directory
    delay = 0
    cmd = cfg.get("DEFAULT", "leading_runsolver_info") + \
          " runsolver -o %s --timestamp --use-pty -W %d -C %d -M %d -d %d %s" \
          % (run_instance_output, time_limit, cpu_limit, memory_limit, delay,
             python_cmd)
    return cmd


def get_parameters():
    params = dict(zip(sys.argv[6::2], sys.argv[7::2]))
    remove_param_metadata(params)
    params = OrderedDict(sorted(params.items(), key=lambda t: t[0]))
    return params


def parse_output_files(cfg, run_instance_output, runsolver_output_file):
    cpu_time, wallclock_time, error = read_runsolver_output(
        runsolver_output_file)
    result_array, result_string = read_run_instance_output(run_instance_output)

    if error is None and result_string is None:
        additional_data = "No result string returned. Please have a look " \
                          "at " + run_instance_output
        rval = (cpu_time, wallclock_time, "CRASHED", cfg.getfloat("DEFAULT",
                "result_on_terminate"), additional_data)
        os.remove(runsolver_output_file)

    elif error is None and result_array[3] != "SAT":
        additional_data = "Please have a look at " + run_instance_output + "."\
            "The output status is not \"SAT\""
        rval = (cpu_time, wallclock_time, "CRASHED", cfg.getfloat("DEFAULT",
                "result_on_terminate"), additional_data)
        os.remove(runsolver_output_file)

    elif error is None and not np.isfinite(float(result_array[6].strip(","))):
        additional_data = "Response value is not finite. Please have a look " \
                          "at " + run_instance_output
        rval = (cpu_time, wallclock_time, "UNSAT", cfg.getfloat("DEFAULT",
                "result_on_terminate"), additional_data)

    elif error is None:
        # Remove the run_instance_output only if there is a valid result
        os.remove(run_instance_output)
        os.remove(runsolver_output_file)
        rval = (cpu_time, wallclock_time, "SAT", float(result_array[6].strip(",")),
                cfg.get("DEFAULT", "function"))
        #print result_string

    else:
        rval = (cpu_time, wallclock_time, "CRASHED", cfg.getfloat("DEFAULT",
                "result_on_terminate"), error + " Please have a look at " +
                                        runsolver_output_file)
        # It is useful to have the run_instance_output for debugging
        os.remove(run_instance_output)

    return rval


def format_return_string(status, runtime, runlength, quality, seed,
                         additional_data):
    return_string = "Result for ParamILS: %s, %f, %d, %f, %d, %s" %\
                   (status, runtime, runlength, quality, seed, additional_data)
    return return_string


def main():
    optimizer = get_optimizer()
    # This has to be done here for SMAC, since smac does not call cv.py
    if 'smac' in optimizer:
        cv_starttime = time.time()
        experiment = load_experiment_file()
        experiment.start_cv(cv_starttime)
        del experiment

    cfg = wrapping_util.load_experiment_config_file()

    fold, seed = parse_command_line()
    # Side-effect: removes all additional information like log and applies
    # transformations to the parameters
    params = get_parameters()
    param_string = " ".join([key + " " + params[key] for key in params])

    time_string = wrapping_util.get_time_string()
    run_instance_output = os.path.join(os.getcwd(),
                                       time_string + "_run_instance.out")
    runsolver_output_file = os.path.join(os.getcwd(),
                                         time_string + "_runsolver.out")

    cmd = make_command(cfg, fold, param_string, run_instance_output)

    fh = open(runsolver_output_file, "w")
    experiment = load_experiment_file()
    # Side-effect: adds a job if it is not yet in the experiments file
    trial_index = get_trial_index(experiment, fold, params)
    experiment.set_one_fold_running(trial_index, fold)
    del experiment  # release Experiment lock

    process = subprocess.Popen(cmd, stdout=fh,
                               stderr=fh, shell=True, executable="/bin/bash")
                                    
    print
    print cmd
    print "-----------------------RUNNING RUNSOLVER----------------------------"
    process.wait()
    fh.close()

    cpu_time, wallclock_time, status, result, additional_data = \
        parse_output_files(cfg, run_instance_output, runsolver_output_file)

    experiment = load_experiment_file()
    if status == "SAT":
        experiment.set_one_fold_complete(trial_index, fold, result,
                                         wallclock_time)
    elif status == "CRASHED" or status == "UNSAT":
        result = cfg.getfloat("DEFAULT", "result_on_terminate")
        experiment.set_one_fold_crashed(trial_index, fold, result,
                                        wallclock_time)
    else:
        # TODO: We need a global stopping mechanism
        pass
    del experiment  #release lock

    return_string = format_return_string(status, wallclock_time, 1, result,
                                         seed, additional_data)

    if 'smac' in optimizer:
        experiment = load_experiment_file()
        experiment.end_cv(time.time())
        del experiment

    print return_string
    return return_string
        
if __name__ == "__main__":
    main()
