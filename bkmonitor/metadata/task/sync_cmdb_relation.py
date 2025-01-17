# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import json
import logging
import time

from django.conf import settings
from django.db import transaction

from alarm_backends.core.lock.service_lock import share_lock
from bkmonitor.utils.cipher import transform_data_id_to_token
from core.prometheus import metrics
from metadata.models import (
    ClusterInfo,
    DataSource,
    Label,
    ResultTable,
    Space,
    TimeSeriesGroup,
)
from metadata.tools.constants import TASK_FINISHED_SUCCESS, TASK_STARTED
from metadata.utils.redis_tools import RedisTools

logger = logging.getLogger("metadata")


@share_lock(ttl=3600, identify="metadata_sync_relation_redis_data")
def sync_relation_redis_data():
    """
    同步cmdb-relation内置数据
    """
    logger.info("sync_relation_redis_data started")
    start_time = time.time()
    # 统计&上报 任务状态指标
    metrics.METADATA_CRON_TASK_STATUS_TOTAL.labels(
        task_name="sync_relation_redis_data", status=TASK_STARTED, process_target=None
    ).inc()
    # 获取对应的Redis数据
    redis_key = settings.BUILTIN_DATA_RT_REDIS_KEY
    redis_data = RedisTools.hgetall(redis_key)
    # 批量获取所有内置RT对象
    existing_rts = ResultTable.objects.filter(is_builtin=True)
    existing_rts_dict = {rt.table_id: rt for rt in existing_rts}
    for field, value in redis_data.items():
        value_dict = json.loads(value)  # 获取对应的field与value
        key = field.decode('utf-8')
        space_type, space_id = key.split('__')  # 分割出space_type和space_id
        # 转义出对应的业务ID，容器等非业务类型ID为负数
        biz_id = space_id if space_type == "bkcc" else Space.objects.get_biz_id_by_space(space_type, space_id)
        # {biz_id}_{space_type}_built_in_time_series.__default__
        data_name = "{}_{}_built_in_time_series".format(biz_id, space_type)
        table_id = "{}_{}_built_in_time_series.__default__".format(biz_id, space_type)  # table_id有限制，必须以业务ID数字开头
        token = value_dict.get('token')
        modify_time = value_dict.get('modifyTime')  # noqa
        logger.info("sync_relation_redis_data start sync builtin redis data, field={}".format(key))

        rt = existing_rts_dict.get(table_id)
        if rt:
            if not token:  # RT存在，但是token不存在场景 -> 生成对应Token，写入Redis
                try:
                    logger.info(
                        "sync_relation_redis_data Field {} RT exist but token is empty,start generate token".format(key)
                    )
                    ds = DataSource.objects.get(data_name=data_name)
                    data_id = ds.bk_data_id
                    new_modify_time = str(int(time.time()))
                    generated_token = transform_data_id_to_token(
                        metric_data_id=data_id, bk_biz_id=biz_id, app_name=data_name
                    )
                    # 更新Redis中的Token和modifyTime
                    value_dict['token'] = generated_token
                    # DS中的Token也需要更新
                    ds = DataSource.objects.get(bk_data_id=data_id)
                    ds.token = generated_token
                    ds.save()
                    value_dict['modifyTime'] = new_modify_time
                    RedisTools.hset_to_redis(redis_key, key, json.dumps(value_dict))
                    logger.info("sync_relation_redis_data Generate Token For Field {} has completed".format(key))
                except Exception as e:
                    logger.error(
                        f"sync_relation_redis_data error: Failed to write data to redis, error={e}, field={key}"
                    )
        else:
            if not token:  # RT不存在，Token不存在场景 -> 创建新DS&RT -> 写入Redis
                try:
                    logger.info(
                        "Field {} RT not exist and token is empty,start generate token and create new datalink".format(
                            key
                        )
                    )
                    with transaction.atomic():
                        # field下对应RT不存在且Token不存在，创建新DS与RT,使用事务保证实例同时成功创建
                        ds = DataSource.create_data_source(
                            data_name=data_name,
                            operator="system",
                            type_label="time_series",
                            source_label="bk_monitor",
                            etl_config="bk_standard_v2_time_series",
                            space_type_id=space_type,
                            space_uid=key,
                        )
                        new_rt = TimeSeriesGroup.create_time_series_group(
                            bk_data_id=ds.bk_data_id,
                            bk_biz_id=biz_id,
                            time_series_group_name=data_name,
                            label=Label.RESULT_TABLE_LABEL_OTHER,
                            operator="system",
                            table_id=table_id,
                            is_builtin=True,
                            default_storage_config={
                                ClusterInfo.TYPE_INFLUXDB,
                            },
                        )
                        generated_token = transform_data_id_to_token(
                            metric_data_id=ds.bk_data_id,
                            bk_biz_id=biz_id,
                            app_name=data_name,
                        )
                    ds.token = generated_token
                    ds.save()
                    # 更新Redis中的Token和modifyTime
                    value_dict['token'] = generated_token
                    value_dict['modifyTime'] = new_rt.last_modify_time
                    RedisTools.hset_to_redis(redis_key, key, json.dumps(value_dict))
                except Exception as e:
                    logger.error(f"sync_relation_redis_data error: Failed to create new DS&RT, error={e}, field={key}")
                    continue

    cost_time = time.time() - start_time
    metrics.METADATA_CRON_TASK_STATUS_TOTAL.labels(
        task_name="sync_relation_redis_data", status=TASK_FINISHED_SUCCESS, process_target=None
    ).inc()
    metrics.METADATA_CRON_TASK_COST_SECONDS.labels(task_name="sync_relation_redis_data", process_target=None).observe(
        cost_time
    )
    metrics.report_all()
    logger.info("sync_relation_redis_data finished successfully,use->[%s] seconds", cost_time)
