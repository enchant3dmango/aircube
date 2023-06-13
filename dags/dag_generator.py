import os
import yaml

from generators import mysql_to_bq, postgres_to_bq
from plugins.utils.dag_config_reader import get_yaml_config_files
from plugins.constants.variable import BRONZE, MYSQL_TO_BQ, POSTGRES_TO_BQ, SILVER

config_files = get_yaml_config_files(os.getcwd(), '*.yaml')


def dynamic_dag(config, dag_id):
    if MYSQL_TO_BQ in config.get('dag')['type']:
        mysql_to_bq.generate_dag(config, dag_id)
    elif POSTGRES_TO_BQ in config.get('dag')['type']:
        postgres_to_bq.generate_dag(config, dag_id)


def update_dynamic_dag():
    for config_file in config_files():
        with open(config_file) as file:
            config = yaml.safe_load(file)
        if BRONZE in config.get('dag')['type']:
            dag_id = f'{BRONZE}_{config.get("database")["name"]}.{config.get("database")["table"]}'
        elif SILVER in config.get('dag')['type']:
            dag_id = f'{SILVER}_{config.get("database")["name"]}.{config.get("database")["table"]}'

    dynamic_dag(config=config, dag_id=dag_id)


update_dynamic_dag()
