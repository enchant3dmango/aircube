#
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
"""
Example LatestOnlyOperator and TriggerRule interactions
"""
from __future__ import annotations

import datetime
import pendulum

from airflow.operators.empty import EmptyOperator
from airflow.operators.latest_only import LatestOnlyOperator
from airflow.utils.trigger_rule import TriggerRule
from airflow.decorators import dag, task


@dag(dag_id="latest_only_with_trigger",schedule=datetime.timedelta(hours=4), start_date=pendulum.datetime(2021, 1, 1, tz="UTC"), catchup=False, tags=["example"])
def dynamic_generated_dag():
    @task
    def latest_only():
        task = LatestOnlyOperator(task_id="latest_only")
        return task
    
    @task
    def task1():
        task = EmptyOperator(task_id="task1")
        return task

    @task
    def task2():
        task = EmptyOperator(task_id="task2")
        return task

    @task
    def task3():
        task = EmptyOperator(task_id="task3")
        return task

    @task
    def task4():
        task = EmptyOperator(task_id="task4", trigger_rule=TriggerRule.ALL_DONE)
        return task

    latest_only() >> task1() >> task3() >> task4()
