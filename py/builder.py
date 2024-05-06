#!/usr/bin/env python3
################################################################################
# @file   builder.py
# @author Jay Convertino(johnathan.convertino.1@us.af.mil)
# @date   2024.04.22
# @brief  parse yaml file to execute build tools
#
# @license MIT
# Copyright 2024 Jay Convertino
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
################################################################################
import yaml
import subprocess
import os
import pathlib
import shutil
import sys
import logging
import threading
import re
import time

try:
  import progressbar
except ImportError:
  print("REQUIREMENT MISSING: progressbar2, pip install progressbar2")
  exit(0)

logger = logging.getLogger(__name__)

class bob:
  def __init__(self, yaml_data, target):
    self._yaml_data = yaml_data
    self._target = target
    # template strings for commands
    self._command_template = {
      'fusesoc':    { 'cmd_1' : ["fusesoc", "--cores-root", "{path}", "run", "--build", "--work-root", "output/hdl/{_project_name}", "--target", "{target}", "{project}"]},
      'buildroot':  { 'cmd_1' : ["make", "-C", "{path}", "clean", "all"], 'cmd_2' : ["make", "O={_pwd}/output/linux/{_project_name}", "-C", "{path}", "{config}"], 'cmd_3' : ["make", "O={_pwd}/output/linux/{_project_name}", "-C", "{path}"]},
      'script':     { 'cmd_1' : ["{exec}", "{file}", "{_project_name}", "{args}"]},
      'genimage':   { 'cmd_1' : ["mkdir", "-p", "{_pwd}/output/genimage/tmp/{_project_name}"], 'cmd_2' : ["genimage", "--config", "{path}/{_project_name}.cfg"]}
    }
    self._projects = None
    self._threads  = []
    self._failed = False
    self._thread_lock = None
    self._items = 0
    self._items_done = 0
    self._bar = None
    self._project_name = "None"

  # run the steps to build parts of targets
  def run(self):
    try:
      self._process()
    except Exception as e: raise

    try:
      self._execute()
    except Exception as e: raise

  def list(self):
    print('\n' + f"AVAILABLE YAML COMMANDS FOR BUILD" + '\n')
    for tool, commands in self._command_template.items():
      options = []
      for command, method in commands.items():
        options.extend([word for word in method if word.count('}')])

      str_options = ' '.join(options)

      str_options = str_options.replace('{_project_name}', '')

      str_options = str_options.replace('{_pwd}', '')

      str_options = re.findall(r'\{(.*?)\}', str_options)

      filter_options = list(set(str_options))

      print(f"COMMAND: {tool:<16} OPTIONS: {filter_options}")

  # create dict of dicts that contains lists with lists of lists to execute with subprocess
  # {'project': { 'concurrent': [[["make", "def_config"], ["make"]], [["fusesoc", "run", "--build", "--target", "zed_blinky", "::blinky:1.0.0"]]], 'sequential': [[]]}}
  def _process(self):

    #filter target into updated dictionary if it was selected
    if self._target != None:
      try:
        self._yaml_data = { self._target: self._yaml_data[self._target]}
      except KeyError:
        logger.error(f"Target: {self._target}, does not exist.")
        return ~0

    self._projects = {}

    for project, parts in self._yaml_data.items():
      project_run_type = {}

      for run_type, part in parts.items():
        project_parts = []

        for part, command in part.items():
          try:
            command_template = self._command_template[part].values()
          except KeyError:
            self._failed = True
            raise Exception(f"No build rule for part: {part}.")

          command.update({'_pwd' : os.getcwd()})

          command.update({'_project_name' : project})

          part_commands = []

          for commands in command_template:
            populate_command = []

            string_command = ' '.join(commands)

            list_command = list(string_command.format_map(command).split(" "))

            part_commands.append(list_command)

            logger.debug(part_commands)

          project_parts.append(part_commands)

        project_run_type[run_type] = project_parts

      self._projects[project] = project_run_type

      logger.info(f"Added commands for project: {project}")

    return 0

  #call subprocess as a thread and add it to a list of threads for wait to check on.
  #iterate over projects avaiable and execute commands per project
  def _execute(self):
    if self._projects == None:
      logger.error("NO PROJECTS AVAILABLE FOR BUILDER")
      return ~0

    self._items = self._projects_cmd_count()

    threading.excepthook = self._thread_exception

    self._thread_lock = threading.Lock()

    bar_thread = threading.Thread(target=self._bar_thread, name="bar")

    bar_thread.start()

    for project, run_types in self._projects.items():
      logger.info(f"Starting build for project: {project}")

      self._threads.clear()

      self._project_name = project

      for run_type, commands in run_types.items():
        if run_type == 'concurrent':
          for command_list in commands:
            logger.debug("CONCURRENT: " + str(command_list))
            thread = threading.Thread(target=self._subprocess, name=project, args=[command_list])

            self._threads.append(thread)

            thread.start()

          for t in self._threads:
            t.join()

          if self._failed:
            raise Exception(f"ERROR executing command list: {' '.join(command_list)}")

        elif run_type == 'sequential':
          for command_list in commands:
            logger.debug("SEQUENTIAL: " + str(command_list))

            try:
              self._subprocess(command_list)
            except Exception as e:
              self._failed = True
              raise

        else:
          logger.error(f"RUN_TYPE {run_type} is not a valid selection")

    bar_thread.join()

  def _subprocess(self, list_of_commands):
    for command in list_of_commands:
      result = None

      try:
        logger.info(f"Executing command: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, check=True, text=True, cwd=str(pathlib.Path.cwd()))
      except subprocess.CalledProcessError as error_code:
        logger.error(str(error_code))

        for line in error_code.stderr.split('\n'):
          if len(line):
            logger.error(line)

        raise Exception(f"ERROR executing command: {' '.join(command)}")

      for line in result.stdout.split('\n'):
        if len(line):
          logger.debug(line)

      with self._thread_lock:
        self._items_done = self._items_done + 1

        time.sleep(1)

        logger.info(f"Completed command: {' '.join(command)}")

  def _projects_cmd_count(self):
    count = 0

    for project, run_types in self._projects.items():
      for run_type, commands in run_types.items():
        for command_list in commands:
          count = count + (len(command_list))

    return count

  def _thread_exception(self, args):
    self._failed = True
    logger.error("Thread failed, allowing current threads to finish and then ending builds.")

  def _bar_thread(self):
    self._bar = progressbar.ProgressBar(widgets=[progressbar.RotatingMarker(), " ", progressbar.Percentage(), " ", progressbar.GranularBar(markers=' ░▒▓█', left='', right='| '), progressbar.Variable('Building')], max_value=self._items).start()

    while((self._items_done < self._items) and (self._failed == False)):
      time.sleep(0.1)
      self._bar.update(Building=self._project_name)
      self._bar.update(self._items_done)

    self._bar.finish()

