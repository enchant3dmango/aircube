import json
import logging
import os
from typing import List

import pendulum
import yaml
from airflow.hooks.base import BaseHook
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import \
    SparkKubernetesOperator
from airflow.providers.cncf.kubernetes.sensors.spark_kubernetes import \
    SparkKubernetesSensor
from airflow.providers.mysql.hooks.mysql import MySqlHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from google.cloud import bigquery

from plugins.constants.types import (DELSERT, EXTENDED_SCHEMA, MYSQL_TO_BQ,
                                     POSTGRES_TO_BQ, PYTHONPATH,
                                     SPARK_KUBERNETES_OPERATOR,
                                     SPARK_KUBERNETES_SENSOR, UPSERT)
from plugins.constants.variables import (RDBMS_TO_BQ_APPLICATION_FILE,
                                         SPARK_JOB_NAMESPACE)
from plugins.task_generators.rdbms_to_bq.types import (
    DELSERT_QUERY, SOURCE_EXTRACT_QUERY, TEMP_TABLE_PARTITION_DATE_QUERY,
    UPSERT_QUERY)
from plugins.utils.miscellaneous import get_escaped_string


class RdbmsToBq:
    def __init__(self, dag_id: str, config: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dag_id                    : str             = dag_id
        self.bq_client                 : bigquery.Client = bigquery.Client()
        self.task_type                 : str             = config['type']
        self.source_connection         : str             = config['source']['connection']
        self.source_schema             : str             = config['source']['schema']
        self.source_table              : str             = config['source']['table']
        self.source_timestamp_keys     : List[str]       = config['source']['timestamp_keys']
        self.source_unique_keys        : List[str]       = config['source']['unique_keys']
        self.target_bq_project         : str             = config['target']['bq']['project']
        self.target_bq_dataset         : str             = config['target']['bq']['dataset']
        self.target_bq_table           : str             = config['target']['bq']['table']
        self.target_bq_load_method     : str             = config['target']['bq']['load_method']
        self.target_bq_partition_key   : str             = config['target']['bq']['partition_key']
        self.target_bq_table_temp      : str             = f'{self.target_bq_table}_temp'
        self.full_target_bq_table      : str             = f'{self.target_bq_project}.{self.target_bq_dataset}.{self.target_bq_table}'
        self.full_target_bq_table_temp : str             = f'{self.target_bq_project}.{self.target_bq_dataset}.{self.target_bq_table_temp}'
        self.target_gcs_project        : str             = config['target']['gcs']['project']
        self.target_gcs_bucket         : str             = config['target']['gcs']['bucket']

        try:
            if self.task_type == POSTGRES_TO_BQ:
                self.sql_hook = PostgresHook(
                    postgres_conn_id = self.source_connection)
                self.quoting = lambda text: f"'{text}'"
            elif self.task_type == MYSQL_TO_BQ:
                self.sql_hook = MySqlHook(
                    mysql_conn_id = self.source_connection)
                self.quoting = lambda text: f'`{text}`'
        except:
            logging.exception('Task type is not supported!')

    def __generate_schema(self, **kwargs) -> list:
        schema_path = f'{os.environ["PYTHONPATH"]}/dags/{self.target_bq_dataset}/{self.target_bq_table}'
        schema_file = os.path.join(schema_path, "assets/schema.json")

        try:
            logging.info(f'Getting table schema from {schema_file}')
            with open(schema_file, "r") as file:
                schema = json.load(file)
                file.close()

            fields = [schema_detail["name"] for schema_detail in schema]
            schema.extend(
                [
                    schema_detail
                    for schema_detail in EXTENDED_SCHEMA
                    if schema_detail["name"] not in fields
                ]
            )
        except:
            raise Exception("Failed to generate schema!")

        return schema

    def __generate_extract_query(self, schema: list, **kwargs) -> str:
        # Get all field name and the extended field name
        extended_fields = [schema_detail["name"]
                           for schema_detail in EXTENDED_SCHEMA]
        fields = [
            schema_detail["name"]
            for schema_detail in schema
            if schema_detail["name"] not in extended_fields
        ]

        load_timestamp = pendulum.now('Asia/Jakarta')

        # Generate query
        source_extract_query = SOURCE_EXTRACT_QUERY.substitute(
            selected_fields=', '.join([self.quoting(field)
                                       for field in fields]),
            load_timestamp=load_timestamp,
            source_schema=self.quoting(self.source_schema),
            source_table_name=self.quoting(self.source_table),
        )

        # Generate query filter based on target_bq_load_method
        if self.target_bq_load_method == UPSERT or self.target_bq_load_method == DELSERT:
            # Create the condition for filtering based on timestamp_keys
            condition = ' OR '.join(
                [
                    f"{timestamp_key} >=  {{{{ data_interval_start.astimezone(dag.timezone) }}}} AND {timestamp_key} < {{{{ data_interval_end.astimezone(dag.timezone) }}}}"
                    for timestamp_key in self.source_timestamp_keys
                ]
            )
            query = source_extract_query + f" WHERE {condition}"

        logging.info(f'Extract query: {query}')

        return get_escaped_string(query)

    def __generate_merge_query(self, schema, **kwargs) -> str:
        audit_condition = ''
        query = None

        # Query to get partition_key date list from temp table to be used as audit condition in DELSERT_QUERY
        if self.target_bq_partition_key is not None:
            temp_table_partition_date_query = TEMP_TABLE_PARTITION_DATE_QUERY.substitute(
                partition_key=self.target_bq_partition_key,
                target_bq_table_temp=self.full_target_bq_table_temp
            )
            audit_condition = f"AND DATE(x.{self.target_bq_partition_key}) IN UNNEST(formatted_dates)"

        if self.target_bq_load_method == DELSERT:
            merge_query = DELSERT_QUERY.substitute(
                target_bq_table=self.full_target_bq_table,
                target_bq_table_temp=self.full_target_bq_table_temp,
                on_keys=' AND '.join(
                    [f"COALESCE(CAST(x.`{key}` as string), 'NULL') = COALESCE(CAST(y.`{key}` as string), 'NULL')" for key in self.source_unique_keys]),
                audit_condition=audit_condition,
                insert_fields=', '.join([f"`{field['name']}`" for field in schema])
            )

            query = temp_table_partition_date_query + merge_query
            logging.info(f'Delsert query: {query}')

        elif self.target_bq_load_method == UPSERT:
            query = UPSERT_QUERY.substitute(
                target_bq_table=self.full_target_bq_table,
                target_bq_table_temp=self.full_target_bq_table_temp,
                on_keys=' AND '.join(
                    [f"COALESCE(CAST(x.`{key}` as string), 'NULL') = COALESCE(CAST(y.`{key}` as string), 'NULL')" for key in self.source_unique_keys]),
                update_fields=', '.join(
                    [f"x.`{field['name']}` = y.`{field['name']}`" for field in schema]),
                insert_fields=', '.join([f"`{field['name']}`" for field in schema])
            )
            logging.info(f'Upsert query: {query}')

        return get_escaped_string(query)

    def __generate_jdbc_uri(self, **kwargs) -> str:
        return f'jdbc:{BaseHook.get_connection(self.source_connection).get_uri()}'

    def __generate_jdbc_url(self, **kwargs) -> str:
        return self.__generate_jdbc_uri().split("//")[1].split("@")[1]

    def __generate_jdbc_credential(self, **kwargs) -> List[str]:
        credential = BaseHook.get_connection(self.source_connection)

        return f'{credential.login}:{credential.password}'

    def generate_task(self):
        schema = self.__generate_schema()

        with open(f'{PYTHONPATH}/{RDBMS_TO_BQ_APPLICATION_FILE}') as f:
            application_file = yaml.safe_load(f)

        application_file['spec']['arguments'] = [
            f"--target_bq_load_method={self.target_bq_load_method}",
            f"--source_timestamp_keys={','.join(self.source_timestamp_keys)}",
            f"--jdbc_credential={self.__generate_jdbc_credential()}",
            f"--partition_key={self.target_bq_partition_key}",
            f"--extract_query={self.__generate_extract_query(schema=schema)}",
            f"--merge_query={self.__generate_merge_query(schema=schema)}",
            f"--task_type={self.task_type}",
            f"--jdbc_url={self.__generate_jdbc_url()}",
            f"--schema={get_escaped_string(json.dumps(self.__generate_schema(), separators=(',', ':')))}",
        ]

        spark_kubernetes_operator_task_id = f'{self.target_bq_dataset.replace("_", "-")}-{self.target_bq_table.replace("_", "-")}-{SPARK_KUBERNETES_OPERATOR}'
        spark_kubernetes_operator_task = SparkKubernetesOperator(
            task_id          = spark_kubernetes_operator_task_id,
            application_file = yaml.safe_dump(application_file),
            namespace        = SPARK_JOB_NAMESPACE,
            do_xcom_push     = True,
        )

        spark_kubernetes_sensor_task = SparkKubernetesSensor(
            task_id          = f"{self.target_bq_dataset.replace('_', '-')}-{self.target_bq_table.replace('_', '-')}-{SPARK_KUBERNETES_SENSOR}",
            namespace        = SPARK_JOB_NAMESPACE,
            application_name = f"{{{{ task_instance.xcom_pull(task_ids={spark_kubernetes_operator_task_id})['metadata']['name'] }}}}",
            attach_log       = True
        )

        return spark_kubernetes_operator_task >> spark_kubernetes_sensor_task