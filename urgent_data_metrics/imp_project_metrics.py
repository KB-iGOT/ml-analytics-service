# -----------------------------------------------------------------
# Name : pyspark_project_batch.py
# Author :Shakthiehswari, Ashwini
# Description : Extracts the Status of the Project submissions 
#  either Started / In-Progress / Submitted along with the users 
#  entity information
# -----------------------------------------------------------------

import json, sys, time
from configparser import ConfigParser,ExtendedInterpolation
from pymongo import MongoClient
from bson.objectid import ObjectId
import os
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
import pyspark.sql.functions as F
from pyspark.sql.types import *
from pyspark.sql import Row
from collections import OrderedDict, Counter
import datetime
from datetime import date
from pyspark.sql import DataFrame
from typing import Iterable
from pyspark.sql.functions import element_at, split, col
import logging
import logging.handlers
from logging.handlers import TimedRotatingFileHandler
import glob , requests

config_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))
config = ConfigParser(interpolation=ExtendedInterpolation())
config.read(config_path[0] + "/config.ini")
sys.path.append(config.get("COMMON", "cloud_module_path"))

# from cloud import MultiCloud

# cloud_init = MultiCloud()
formatter = logging.Formatter('%(asctime)s - %(levelname)s')

successLogger = logging.getLogger('success log')
successLogger.setLevel(logging.DEBUG)

# Add the log message handler to the logger
successHandler = logging.handlers.RotatingFileHandler(
    config.get('LOGS', 'project_success')
)
successBackuphandler = TimedRotatingFileHandler(
    config.get('LOGS','project_success'),
    when="w0",
    backupCount=1
)
successHandler.setFormatter(formatter)
successLogger.addHandler(successHandler)
successLogger.addHandler(successBackuphandler)

errorLogger = logging.getLogger('error log')
errorLogger.setLevel(logging.ERROR)
errorHandler = logging.handlers.RotatingFileHandler(
    config.get('LOGS', 'project_error')
)
errorBackuphandler = TimedRotatingFileHandler(
    config.get('LOGS', 'project_error'),
    when="w0",
    backupCount=1
)
errorHandler.setFormatter(formatter)
errorLogger.addHandler(errorHandler)
errorLogger.addHandler(errorBackuphandler)

try:
    def convert_to_row(d: dict) -> Row:
        return Row(**OrderedDict(sorted(d.items())))
except Exception as e:
    errorLogger.error(e, exc_info=True)

try:
    def removeduplicate(it):
        seen = []
        for x in it:
            if x not in seen:
                yield x
                seen.append(x)
except Exception as e:
    errorLogger.error(e, exc_info=True)

try:
 def melt(df: DataFrame,id_vars: Iterable[str], value_vars: Iterable[str],
        var_name: str="variable", value_name: str="value") -> DataFrame:

    _vars_and_vals = array(*(
        struct(lit(c).alias(var_name), col(c).alias(value_name))
        for c in value_vars))

    # Add to the DataFrame and explode
    _tmp = df.withColumn("_vars_and_vals", explode(_vars_and_vals))

    cols = id_vars + [
            col("_vars_and_vals")[x].alias(x) for x in [var_name, value_name]]
    return _tmp.select(*cols)
except Exception as e:
   errorLogger.error(e,exc_info=True)

spark = SparkSession.builder.appName("nvsk").config(
    "spark.driver.memory", "50g"
).config(
    "spark.executor.memory", "100g"
).config(
    "spark.memory.offHeap.enabled", True
).config(
    "spark.memory.offHeap.size", "32g"
).getOrCreate()

sc = spark.sparkContext

clientProd = MongoClient(config.get('MONGO', 'mongo_url'))
db = clientProd[config.get('MONGO', 'database_name')]
projectsCollec = db[config.get('MONGO', 'projects_collection')]

projects_cursorMongo = projectsCollec.aggregate(
      [{"$match":{"isAPrivateProgram":False,"isDeleted":False,"programInformation.name":{"$regex": "^((?!(?i)(test)).)*$"}}},
{
        "$project": {
            "_id": {"$toString": "$_id"},
            "status": 1,
            "attachments":1,
            "tasks": {"attachments":1,"_id": {"$toString": "$_id"}},
            "userProfile": 1,
            "userRoleInformation" : {"district":1,"state": 1},
        }
    }]
)

projects_schema = StructType([
    StructField('_id', StringType(), True),
    StructField('status', StringType(), True),
    StructField(
        'attachments',
        ArrayType(
            StructType([StructField('sourcePath', StringType(), True)])
        ), True
    ),
    StructField(
        'tasks',
        ArrayType(
            StructType([StructField('_id', StringType(), True),
                       StructField('attachments',
                                    ArrayType(
                                        StructType([StructField('sourcePath', StringType(), True)])
        ), True)])
        ), True
    ),
    StructField(
          'userProfile',
          StructType([
          StructField(
              'userLocations', ArrayType(
                  StructType([
                     StructField('name', StringType(), True),
                     StructField('type', StringType(), True),
                     StructField('id', StringType(), True),
                     StructField('code', StringType(), True)
                  ]),True)
          )
          ])
    ),
    StructField("userRoleInformation", StructType([
        StructField("district", StringType(), True),
        StructField("state", StringType(), True)
    ]), True),
])



def searchEntities(url,ids_list):
    try:
        headers = {
          'Authorization': config.get('API_HEADERS', 'authorization_access_token'),
          'content-Type': 'application/json'
        }
        # prepare api body 
        payload = json.dumps({
          "request": {
            "filters": {
              "id": ids_list
            }
          }
        })
        response = requests.request("POST", url, headers=headers, data=payload)
        delta_ids = []
        entity_name_mapping = {}
        if response.status_code == 200:
            # convert the response to dictionary 
            response = response.json()

            data = response['result']['response']
            
            entity_name_mapping = {}
            # prepare entity name - id mapping
            for index in data:
                entity_name_mapping[index['id']] = index['name']

            # fetch the ids from the mapping 
            ids_from_api = list(entity_name_mapping.keys())

            # check with the input data to make sure there are no missing data from loc search 
            delta_ids = list(set(ids_list) - set(ids_from_api))
        else :
            delta_ids = ids_list
        # if there are missing data , fetch the data from mongo 
        if len(delta_ids) > 0 :
          # aggregate mongo query to fetch data from mongo 
          delta_loc = projectsCollec.aggregate([
                          {
                            "$match": {
                              "userProfile.userLocations": {
                                "$elemMatch": {
                                  "id": {
                                    "$in": delta_ids
                                  }
                                }
                              }
                            }
                          },
                          {
                            "$unwind": "$userProfile.userLocations"
                          },
                          {
                            "$match": {
                              "userProfile.userLocations.id": {
                                "$in": delta_ids
                              }
                            }
                          },
                          {
                            "$sort": {
                              "createdAt": -1
                            }
                          },
                          {
                            "$group": {
                              "_id": "$userProfile.userLocations.id",
                              "mostRecentDocument": { "$first": "$$ROOT" }
                            }
                          },
                          {
                            "$replaceRoot": { "newRoot": "$mostRecentDocument" }
                          },
                          {
                            "$project": {
                              "_id": 1,
                              "userProfile.userLocations": 1
                            }
                          }
                      ])
              # add delta entities to master variable
          for index in delta_loc:
            entity_name_mapping[index['userProfile']["userLocations"]['id']] = index['userProfile']["userLocations"]['name']
          return entity_name_mapping
        else:
            return False
        
    except Exception as e:
       errorLogger.error(e,exc_info=True)

projects_df = spark.createDataFrame(projects_cursorMongo,projects_schema)
projects_df = projects_df.withColumn(
                 "project_evidence_status",
                 F.when(
                      size(F.col("attachments"))>=1,True
                 ).otherwise(False)
              )
projects_df = projects_df.withColumn("exploded_tasks", F.explode_outer(F.col("tasks")))

projects_df = projects_df.withColumn(
                 "task_evidence_status",
                 F.when(
                      size(projects_df["exploded_tasks"]["attachments"])>=1,True
                 ).otherwise(False)
              )

projects_df = projects_df.withColumn(
                 "evidence_status",
                F.when(
                      (projects_df["project_evidence_status"]== False) & (projects_df["task_evidence_status"]==False),False
                 ).otherwise(True)
              )

projects_df = projects_df.withColumn(
   "exploded_userLocations",F.explode_outer(projects_df["userProfile"]["userLocations"])
)

entities_df = melt(projects_df,
        id_vars=["_id","exploded_userLocations.name","exploded_userLocations.type","exploded_userLocations.id","userRoleInformation.district","userRoleInformation.state"],
        value_vars=["exploded_userLocations.code"]
    ).select("_id","name","value","type","id","district","state").dropDuplicates()

projects_df = projects_df.join(entities_df,projects_df["_id"]==entities_df["_id"],how='left')\
        .drop(entities_df["_id"])
projects_df = projects_df.filter(F.col("status") != "null")

entities_df.unpersist()


projects_df_final = projects_df.select(
              projects_df["_id"].alias("project_id"),
              projects_df["status"],
              projects_df["evidence_status"],
              projects_df["district"],
              projects_df["state"],
           )
projects_df_final = projects_df_final.dropDuplicates()

district_final_df = projects_df_final.groupBy("state","district")\
    .agg(countDistinct(F.col("project_id")).alias("Total_Micro_Improvement_Projects"),countDistinct(when(F.col("status") == "started",True)\
    ,F.col("project_id")).alias("Total_Micro_Improvement_Started"),countDistinct(when(F.col("status") == "inProgress",True),\
    F.col("project_id")).alias("Total_Micro_Improvement_InProgress"),countDistinct(when(F.col("status") == "submitted",True),\
    F.col("project_id")).alias("Total_Micro_Improvement_Submitted"),\
    countDistinct(when((F.col("evidence_status") == True)&(F.col("status") == "submitted"),True),\
    F.col("project_id")).alias("Total_Micro_Improvement_Submitted_With_Evidence")).sort("state","district")

# select only  district ids from the Dataframe 
district_to_list = projects_df_final.select("district").rdd.flatMap(lambda x: x).collect()
# select only  state ids from the Dataframe 
state_to_list = projects_df_final.select("state").rdd.flatMap(lambda x: x).collect()

# merge the list of district and state ids , remove the duplicates 
ids_list = list(set(district_to_list)) + list(set(state_to_list))

# remove the None values from the list 
ids_list = [value for value in ids_list if value is not None]



# call function to get the entity from location master 
response = searchEntities(config.get("API_ENDPOINTS", "base_url") + config.get("API_ENDPOINTS", "location_search"),ids_list)

if response :
    # Convert dictionary to list of tuples
    data_tuples = list(response.items())

    # Define the schema
    state_schema = StructType([StructField("id", StringType(), True), StructField("state_name", StringType(), True)])
    district_schema = StructType([StructField("id", StringType(), True), StructField("district_name", StringType(), True)])

    # Create a DataFrame
    state_id_mapping = spark.createDataFrame(data_tuples, schema=state_schema)
    
    # Create a DataFrame
    district_id_mapping = spark.createDataFrame(data_tuples, schema=district_schema)
    
    district_final_df = district_final_df.join(state_id_mapping, district_final_df["state"] == state_id_mapping["id"], "left")

    district_final_df = district_final_df.join(district_id_mapping, district_final_df["district"] == district_id_mapping["id"], "left")

    final_data_to_csv = district_final_df.select("state_name","district_name","Total_Micro_Improvement_Projects","Total_Micro_Improvement_Started","Total_Micro_Improvement_InProgress","Total_Micro_Improvement_Submitted","Total_Micro_Improvement_Submitted_With_Evidence").sort("state_name","district_name")

    # DF To file
    local_path = config.get("COMMON", "nvsk_imp_projects_data_local_path")
    blob_path = config.get("COMMON", "nvsk_imp_projects_data_blob_path")
    final_data_to_csv.coalesce(1).write.format("csv").option("header",True).mode("overwrite").save(local_path)
    final_data_to_csv.unpersist()

    # Renaming a file
    path = local_path
    extension = 'csv'
    os.chdir(path)
    result = glob.glob(f'*.{extension}')
    os.rename(f'{path}' + f'{result[0]}', f'{path}' + 'data.csv')

    # Uploading file to Cloud
    # cloud_init.upload_to_cloud(blob_Path = blob_path, local_Path = local_path, file_Name = 'data.csv')

    print("file got uploaded to Cloud.")
    print("DONE")

else:
    try:
        raise ValueError("Entity Search API failed.")
    except Exception as e:
        # Log the custom error message along with exception information
        error_message = "API error: {}".format(e)
        errorLogger.error(error_message, exc_info=True)
        