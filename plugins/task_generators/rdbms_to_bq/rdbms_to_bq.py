import json
import logging
import os
from ast import literal_eval
from typing import List, Union
from urllib.parse import urlencode

import polars as pl
import yaml
from google.cloud.bigquery import (DestinationFormat, SourceFormat,
                                   WriteDisposition)

from airflow.hooks.base import BaseHook
from airflow.models.connection import Connection
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import SparkKubernetesOperator
from airflow.providers.cncf.kubernetes.sensors.spark_kubernetes import SparkKubernetesSensor
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryDeleteTableOperator, BigQueryInsertJobOperator)
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator
from airflow.providers.mysql.hooks.mysql import MySqlHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from plugins.constants.types import (AIRFLOW, APPEND, DATABASE, DELSERT,
                                     EXTENDED_SCHEMA, MERGE, MYSQL_TO_BQ,
                                     POSTGRES_TO_BQ, PYTHONPATH, SPARK,
                                     SPARK_KUBERNETES_OPERATOR,
                                     SPARK_KUBERNETES_SENSOR, UPSERT)
from plugins.constants.variables import (GCP_CONN_ID, GCS_DATA_LAKE_BUCKET,
                                         RDBMS_TO_BQ_APPLICATION_FILE,
                                         SPARK_JOB_NAMESPACE)
from plugins.task_generators.rdbms_to_bq.types import (
    DELSERT_QUERY, SOURCE_EXTRACT_QUERY, TEMP_TABLE_PARTITION_DATE_QUERY,
    UPSERT_QUERY)
from plugins.utilities.gcs import check_folder_existence, upload_multiple_files_from_local
from plugins.utilities.general import get_onelined_string, remote_multiple_files
from plugins.utilities.polars import dataframe_to_file, dataframe_type_casting

class RDBMSToBQGenerator:
    """
    Constructor arguments:
        dag_id (Required[str]):
            The ID of the Directed Acyclic Graph (DAG) to which the generated tasks belong.
        config (Required[dict]):
            A dictionary containing configuration parameters for the data transfer process.
        **kwargs:
            Additional keyword arguments.
    """

    def __init__(self, dag_id: str, config: dict, **kwargs) -> None:
        self.dag_id                              : str                   = dag_id
        self.task_type                           : str                   = config['type']
        self.task_mode                           : str                   = config['mode']
        self.source_connection                   : Union[str, List[str]] = config['source']['connection']
        self.source_schema                       : str                   = config['source']['schema']
        self.source_table                        : str                   = config['source']['table']
        self.source_start_window_expansion_value : int                   = config['source']['start_window_expansion']['value']
        self.source_start_window_expansion_unit  : str                   = config['source']['start_window_expansion']['unit']
        self.source_timestamp_keys               : List[str]             = config['source']['timestamp_keys']
        self.source_unique_keys                  : List[str]             = config['source']['unique_keys']
        self.target_bq_project                   : str                   = config['target']['bq']['project']
        self.target_bq_dataset                   : str                   = config['target']['bq']['dataset']
        self.target_bq_table                     : str                   = config['target']['bq']['table']
        self.target_bq_load_method               : str                   = config['target']['bq']['load_method']
        self.target_bq_partition_field           : str                   = config['target']['bq']['partition_field']
        self.target_bq_cluster_fields            : List[str]             = config['target']['bq']['cluster_fields']
        self.ts_nodash                           : str                   = "{{ data_interval_start.astimezone(dag.timezone).strftime('%Y%m%dT%H%M%S') }}"
        self.full_target_bq_table                : str                   = f'{self.target_bq_project}.{self.target_bq_dataset}.{self.target_bq_table}'
        self.full_target_bq_table_temp           : str                   = f'{self.target_bq_project}.{self.target_bq_dataset}.{self.target_bq_table}_temp__{self.ts_nodash}'

        # Set default value based on task type
        if self.task_type == POSTGRES_TO_BQ:
            self.quoting, self.string_type = lambda text: f'"{text}"', 'TEXT'
        elif self.task_type == MYSQL_TO_BQ:
            self.quoting, self.string_type = lambda text: f'`{text}`', 'CHAR'
        else:
            raise Exception('Task type is not supported!')

        # Set source_start_window_expansion default value, set both value and unit to 0 and minutes if one of them is not provided
        if self.source_start_window_expansion_value is None or self.source_start_window_expansion_unit is None:
            self.source_start_window_expansion_value, self.source_start_window_expansion_unit = 0, 'minutes'

        self.dag_base_path = f'{os.environ["PYTHONPATH"]}/dags/{self.target_bq_dataset}/{self.target_bq_table}'

    def __generate_schema(self, **kwargs) -> list:
        schema_file = os.path.join(self.dag_base_path, "assets/schema.json")

        try:
            logging.info(f'Getting table schema from {schema_file}')
            with open(schema_file, "r") as file:
                schema = json.load(file)

            fields = [schema_detail["name"] for schema_detail in schema]

            # Extend schema from EXTENDED_SCHEMA
            schema.extend(
                [
                    schema_detail
                    for schema_detail in EXTENDED_SCHEMA
                    if schema_detail["name"] not in fields
                ]
            )
        except:
            raise Exception('Failed to generate schema!')

        return schema

    def __generate_extract_query(self, schema: list = None, **kwargs) -> str:
        # Get all field name and exclude the extended field and database field to be selected
        if schema is not None:
            extended_fields = [schema_detail["name"]
                               for schema_detail in EXTENDED_SCHEMA]
            # Wrap extended_fields and DATABASE field into excluded_fields
            excluded_fields = extended_fields; extended_fields.append(DATABASE) 

            selected_fields = [
                # Cast all columns with string type as TEXT or CHAR, except for database field
                f"CAST({self.quoting(schema_detail['name'])} AS {self.string_type}) AS {self.quoting(schema_detail['name'])}"
                if schema_detail["type"] == 'STRING'
                else (
                    # Replace TIMESTAMP value less than or equal to '1900-01-01 00:00:00' with '1900-01-01 00:00:00'
                    f"""CASE
                        WHEN {self.quoting(schema_detail['name'])} <= '1900-01-01 00:00:00'
                        THEN '1900-01-01 00:00:00'
                        ELSE {self.quoting(schema_detail['name'])}
                    END AS {self.quoting(schema_detail['name'])}"""
                    if schema_detail["type"] == 'TIMESTAMP'
                    else self.quoting(schema_detail['name'])
                )
                for schema_detail in schema
                if schema_detail["name"] not in excluded_fields
            ]

        # Generate query
        source_extract_query = SOURCE_EXTRACT_QUERY.substitute(
            selected_fields=', '.join([field for field in selected_fields]),
            load_timestamp='CURRENT_TIMESTAMP' if self.task_type == POSTGRES_TO_BQ else 'CURRENT_TIMESTAMP()',
            source_table_name=self.quoting(self.source_table) if self.source_schema is None else f'{self.quoting(self.source_schema)}.{self.quoting(self.source_table)}'
        )

        # Add custom value for database field based on connection name
        # This is intended for multiple connection dag
        if kwargs.get(DATABASE) is not None:
            database = str(kwargs[DATABASE]).replace(
                'pg_', '').replace('mysql_', '')
            source_extract_query = source_extract_query.replace(
                " FROM",
                f", '{database}' AS {self.quoting('database')} FROM"
            )

        # Generate query filter based on target_bq_load_method
        if self.target_bq_load_method in MERGE.__members__ or self.target_bq_load_method == APPEND:
            # Create the condition for filtering based on timestamp_keys
            condition = ' OR '.join(
                [
                    f"""{self.quoting(timestamp_key)} >=
                    '{{{{ data_interval_start.astimezone(dag.timezone).subtract({self.source_start_window_expansion_unit}={self.source_start_window_expansion_value}) }}}}'
                    AND {self.quoting(timestamp_key)} <
                    '{{{{ data_interval_end.astimezone(dag.timezone) }}}}'"""
                    for timestamp_key in self.source_timestamp_keys
                ]
            )

            # Append extract query condition
            source_extract_query = source_extract_query + f" WHERE {condition}"

        logging.info(f'Extract query: {source_extract_query}')

        if self.task_mode == SPARK:
            source_extract_query = f'({source_extract_query}) AS cte'

        return get_onelined_string(source_extract_query)

    def __extract_and_upload_to_gcs(self, sql_hook: Union[PostgresHook, MySqlHook], **kwargs) -> None:
        schema = kwargs.get('schema', None)
        dirname = kwargs.get('dirname', None)
        filename = kwargs.get('filename', None)
        extract_query = kwargs.get('extract_query', None)
        rows, estimated_size, dataframe = 0, 0, None

        logging.info(f'Onelined extract query: {extract_query}')

        # Create engine and connection
        sqlalchemy_engine = sql_hook.get_sqlalchemy_engine()
        sqlalchemy_connection = sqlalchemy_engine.connect().execution_options(stream_results=True)

        # Fetch data and save into dataframe(s)
        dataframes = pl.read_database(
            query=extract_query,
            connection=sqlalchemy_connection,
            iter_batches=True,
            batch_size=1000000
        )
        logging.info(f"Data fetched successfully.")

        for index, dataframe in enumerate(dataframes):
            dataframe = dataframe_type_casting(
                dataframe=dataframe,
                schema=schema
            )
            rows += len(dataframe); estimated_size += float("{:.3f}".format(dataframe.estimated_size('kb')))

            # Writing dataframe(s) to file(s)
            dataframe_to_file(
                dataframe=dataframe,
                dirname=dirname,
                filename=f'{filename}__{index}',
                extension=DestinationFormat.PARQUET
            )
        logging.info(f"Total dataframe size: {estimated_size}KB.")

        # Close engine and dispose connection
        sqlalchemy_connection.close()
        sqlalchemy_engine.dispose()

        if dataframe is None:
            logging.info('Empty dataframe.')
        else:
            # Upload file(s) from a local directory to GCS
            upload_multiple_files_from_local(
                bucket=GCS_DATA_LAKE_BUCKET,
                dirname=dirname,
            )
            logging.info(f'{rows} row(s) extracted and uploaded to GCS successfully.')
            # Remove file(s) in a local directory
            remote_multiple_files(
                dirname=dirname
            )

    def __check_folder_existence_in_gcs(self, **kwargs):
        bucket = kwargs.get('bucket', None)
        dirname = kwargs.get('dirname', None)

        exists = check_folder_existence(bucket=bucket, dirname=dirname)

        if not exists:
            logging.info("Folder and its file(s) not exists, skipping load_to_bq task.")
            return f'end'
        else:
            logging.info("Folder and its file(s) exists, executing load_to_bq task.")
            return f"load_to_bq"

    def __generate_merge_query(self, schema, **kwargs) -> str:
        table_creation_query_type = 'COPY'
        partition_filter = ''
        query = None

        # Query to get partition_field date list from temp table to be used as partition filter
        if self.target_bq_partition_field is not None:
            temp_table_partition_date_query = TEMP_TABLE_PARTITION_DATE_QUERY.substitute(
                partition_field=self.target_bq_partition_field,
                target_bq_table_temp=self.full_target_bq_table_temp
            )
            partition_filter = f"AND DATE(x.{self.target_bq_partition_field}) IN UNNEST(formatted_dates)"

        # Use cte to get the latest source table data for the merge statement
        # Only applied to dag that has cte_merge.sql in its assets folder
        # Will use the temporary table as merge source if no cte_merge.sql is provided
        merge_cte = os.path.join(self.dag_base_path, "assets/cte_merge.sql")
        if os.path.exists(merge_cte):
            with open(merge_cte, "r") as file:
                merge_source = file.read()
                merge_source = merge_source.format(
                    full_target_bq_table_temp=self.full_target_bq_table_temp
                )

            # Used for table creation if not exists
            # Can't use COPY directly if the merge source is a CTE from the source table
            table_creation_query_type = f'PARTITION BY TIMESTAMP_TRUNC({self.target_bq_partition_field}, DAY) AS'
        else:
            merge_source = f"`{self.full_target_bq_table_temp}`"

        # Construct delsert query
        if self.target_bq_load_method == DELSERT:
            merge_query = DELSERT_QUERY.substitute(
                table_creation_query_type=table_creation_query_type,
                merge_target=self.full_target_bq_table,
                merge_source=merge_source,
                on_keys=' AND '.join(
                    [f"COALESCE(CAST(x.`{key}` as string), 'NULL') = COALESCE(CAST(y.`{key}` as string), 'NULL')" for key in self.source_unique_keys]),
                partition_filter=partition_filter,
                insert_fields=', '.join(
                    [f"`{field['name']}`" for field in schema])
            )

            query = temp_table_partition_date_query + merge_query
            logging.info(f'Delsert query: {query}')

        # Construct upsert query
        elif self.target_bq_load_method == UPSERT:
            merge_query = UPSERT_QUERY.substitute(
                table_creation_query_type=table_creation_query_type,
                merge_target=self.full_target_bq_table,
                merge_source=merge_source,
                on_keys=' AND '.join(
                    [f"COALESCE(CAST(x.`{key}` as string), 'NULL') = COALESCE(CAST(y.`{key}` as string), 'NULL')" for key in self.source_unique_keys]),
                partition_filter=partition_filter,
                update_fields=', '.join(
                    [f"x.`{field['name']}` = y.`{field['name']}`" for field in schema]),
                insert_fields=', '.join(
                    [f"`{field['name']}`" for field in schema])
            )

            query = temp_table_partition_date_query + merge_query
            logging.info(f'Upsert query: {query}')

        return get_onelined_string(f'{query}')

    def __get_conn(self, **kwargs) -> Connection:
        return BaseHook.get_connection(self.source_connection)

    def __generate_jdbc_url(self, **kwargs) -> str:
        jdbc_uri = f'jdbc:{self.__get_conn().get_uri()}'
        jdbc_uri.replace(
            'postgres', 'postgresql') if self.task_type == POSTGRES_TO_BQ else jdbc_uri

        db_type = jdbc_uri.split("://")[0]
        db_conn = jdbc_uri.split("@")[1].split("?")[0]

        return f'{db_type}://{db_conn}?{self.__generate_jdbc_urlencoded_extra()}' if self.__get_conn().extra else f'{db_type}://{db_conn}'

    def __generate_jdbc_credential(self, **kwargs) -> List[str]:
        credential = self.__get_conn()

        return f'{credential.login}:{credential.password}'

    def __generate_jdbc_urlencoded_extra(self, **kwargs):
        extras = self.__get_conn().extra

        return urlencode(literal_eval(extras))

    def generate_tasks(self):
        if self.task_mode == SPARK:
            # Task generator for single connection dag
            if type(self.source_connection) is str:
                schema_string = f'{json.dumps(self.__generate_schema(), separators=(",", ":"))}'
                onelined_schema_string = get_onelined_string(schema_string)

                schema = self.__generate_schema()
                extract_query = self.__generate_extract_query(schema=schema)
                merge_query = self.__generate_merge_query(schema=schema)

                job_config = {
                    "target_bq_load_method": self.target_bq_load_method,
                    "source_timestamp_keys": ','.join(self.source_timestamp_keys),
                    "full_target_bq_table": self.full_target_bq_table,
                    "target_bq_project": self.target_bq_project,
                    "jdbc_credential": self.__generate_jdbc_credential(),
                    "partition_field": self.target_bq_partition_field,
                    "extract_query": extract_query,
                    "merge_query": merge_query,
                    "task_type": self.task_type,
                    "jdbc_url": self.__generate_jdbc_url(),
                    "schema": onelined_schema_string
                }

                with open(f'{PYTHONPATH}/{RDBMS_TO_BQ_APPLICATION_FILE}') as f:
                    application_file = yaml.safe_load(f)

                application_file['spec']['arguments'] = [
                    f"--job_type=jdbc-to-bigquery",
                    f"--job_config={job_config}"
                ]

                spark_kubernetes_base_task_id = f'{self.target_bq_dataset}-{self.target_bq_table}'.replace(
                    '_', '-').replace('bronze-', '')
                spark_kubernetes_operator_task_id = f'{spark_kubernetes_base_task_id}-{SPARK_KUBERNETES_OPERATOR}'
                spark_kubernetes_operator_task = SparkKubernetesOperator(
                    task_id          = spark_kubernetes_operator_task_id,
                    application_file = yaml.safe_dump(application_file),
                    namespace        = SPARK_JOB_NAMESPACE,
                    do_xcom_push     = True
                )

                spark_kubernetes_sensor_task = SparkKubernetesSensor(
                    task_id          = f"{spark_kubernetes_base_task_id}-{SPARK_KUBERNETES_SENSOR}",
                    namespace        = SPARK_JOB_NAMESPACE,
                    application_name = f"{{{{ task_instance.xcom_pull(task_ids='{spark_kubernetes_operator_task_id}')['metadata']['name'] }}}}",
                    attach_log       = True
                )

                return spark_kubernetes_operator_task >> spark_kubernetes_sensor_task

        elif self.task_mode == AIRFLOW:
            schema = self.__generate_schema()
            extract_query = self.__generate_extract_query(schema=schema)
            dirname = f'{self.target_bq_dataset}/{self.target_bq_table}/{self.ts_nodash}'
            filename = f'{self.source_table}'

            # Use WRITE_APPEND if the load method is APPEND, else, use WRITE_TRUNCATE
            write_disposition = WriteDisposition.WRITE_APPEND if self.target_bq_load_method == APPEND else WriteDisposition.WRITE_TRUNCATE

            # TimePartitioning, https://cloud.google.com/bigquery/docs/reference/rest/v2/tables#TimePartitioning
            time_partitioning = {
                "type": "DAY",
                "field": self.target_bq_partition_field
            } if self.target_bq_partition_field else None

            # Clustering, https://cloud.google.com/bigquery/docs/reference/rest/v2/tables#Clustering
            cluster_fields = self.target_bq_cluster_fields if self.target_bq_cluster_fields else None

            # Task generator for single connection dag
            if type(self.source_connection) is str:
                sql_hook = PostgresHook(postgres_conn_id=self.source_connection) if self.task_type == POSTGRES_TO_BQ else MySqlHook(mysql_conn_id=self.source_connection)

                # Extract data from source database, then load to GCS
                extract = PythonOperator(
                    task_id="extract_and_upload_to_gcs",
                    python_callable=self.__extract_and_upload_to_gcs,
                    op_kwargs={
                        "sql_hook": sql_hook,
                        "schema": schema,
                        "dirname": dirname,
                        "filename": filename,
                        "extract_query": extract_query
                    }
                )

            # Task generator for multiple connection dag
            elif type(self.source_connection) is list:
                extract = []

                for index, connection in enumerate(sorted(self.source_connection)):
                    extract_query = self.__generate_extract_query(schema=schema, database=connection)
                    filename = f'{filename}_{index+1}'

                    sql_hook = PostgresHook(postgres_conn_id=connection) if self.task_type == POSTGRES_TO_BQ else MySqlHook(mysql_conn_id=connection)

                    # Extract data from source database, then load to GCS
                    __extract = PythonOperator(
                        task_id=f"extract_and_upload_to_gcs__{index+1}",
                        python_callable=self.__extract_and_upload_to_gcs,
                        op_kwargs={
                            "sql_hook": sql_hook,
                            "schema": schema,
                            "dirname": dirname,
                            "filename": filename,
                            "extract_query": extract_query
                        }
                    )

                    extract.append(__extract)

            check = BranchPythonOperator(
                task_id=f'check_if_file_exists_in_gcs',
                python_callable=self.__check_folder_existence_in_gcs,
                op_kwargs={
                    'bucket': GCS_DATA_LAKE_BUCKET,
                    'dirname': dirname,
                }
            )

            end = EmptyOperator(task_id='end')

            # Directly load the data into BigQuery main table if the load method is TRUNCATE or APPEND, else, load it to temporary table first
            destination_project_dataset_table = f'{self.full_target_bq_table}' if self.target_bq_load_method not in MERGE.__members__ \
                else f'{self.full_target_bq_table_temp}'
            source_object_dir = f'{self.target_bq_dataset}/{self.target_bq_table}/{self.ts_nodash}'

            # Load data from GCS to BigQuery
            load = GCSToBigQueryOperator(
                task_id                           = f'load_to_bq',
                gcp_conn_id                       = GCP_CONN_ID,
                bucket                            = GCS_DATA_LAKE_BUCKET,
                destination_project_dataset_table = destination_project_dataset_table,
                source_objects                    = [f'{source_object_dir}/*.parquet'],
                schema_fields                     = schema,
                source_format                     = SourceFormat.PARQUET,
                write_disposition                 = write_disposition,
                time_partitioning                 = time_partitioning,
                cluster_fields                    = cluster_fields,
                autodetect                        = False
            )

            # Add extra task to merge the data from temporary table into main table if the load method is DELSERT or UPSERT
            if self.target_bq_load_method in MERGE.__members__:
                merge_query = self.__generate_merge_query(schema=schema)

                # JobConfiguration, https://cloud.google.com/bigquery/docs/reference/rest/v2/Job#jobconfiguration
                configuration = {
                    "query": {
                        "query": merge_query,
                        "useLegacySql": False
                    }
                }

                # Merge temporary table into main table
                merge = BigQueryInsertJobOperator(
                    task_id       = f'merge_to_main_table',
                    project_id    = self.target_bq_project,
                    gcp_conn_id   = GCP_CONN_ID,
                    configuration = configuration
                )

                # Delete temporary table
                delete = BigQueryDeleteTableOperator(
                    task_id                = f'delete_temp_table',
                    gcp_conn_id            = GCP_CONN_ID,
                    deletion_dataset_table = destination_project_dataset_table,
                    ignore_if_missing      = True
                )

                # Set merge as load downstream, and delete as merge downstream
                # It's the same as load >> merge >> delete
                load.set_downstream(merge); merge.set_downstream(delete)

            # Return the DAG task flow
            return extract >> check >> [load, end]
