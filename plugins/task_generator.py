from plugins.constants.types import BQ_TO_PARQUET, RDBMS_TO_BQ, GSHEET_TO_BQ
from plugins.task_generators.bq_to_parquet.bq_to_parquet import \
    BQToParquetGenerator
from plugins.task_generators.rdbms_to_bq.rdbms_to_bq import RDBMSToBQGenerator
from plugins.task_generators.gsheet_to_bq.gsheet_to_bq import GSheetToBQGenerator

def generate_tasks(dag_id, config):

    if config['type'] in RDBMS_TO_BQ.__members__:
        rdbms_to_bq = RDBMSToBQGenerator(dag_id=dag_id, config=config)
        rdbms_to_bq.generate_tasks()
    elif config['type'] == BQ_TO_PARQUET:
        bq_to_parquet = BQToParquetGenerator(dag_id=dag_id, config=config)
        bq_to_parquet.generate_tasks()
    elif config['type'] == GSHEET_TO_BQ:
        gsheet_to_bq = GSheetToBQGenerator(dag_id=dag_id, config=config)
        gsheet_to_bq.generate_task()
    # TODO: Add conditional statement for other task type here
