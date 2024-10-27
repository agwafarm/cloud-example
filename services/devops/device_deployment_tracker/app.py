import json
import logging
import os
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import boto3
from pydantic.main import BaseModel

from common import parameters
from common.controller_manager.controller_manager_factory import ControllerManagerFactory
from common.data_layer import device as device_repository
from common.data_layer.device import DeviceId, ConnectionStatus, ControllerId, get_device_by_controller_id
from common.lambda_events import extract_event
from common.support.monitoring import MonitoringClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

scheduler_client = boto3.client('scheduler')
gg_client = boto3.client('greengrass')

monitoring_client = MonitoringClient()
controller_manger_factory = ControllerManagerFactory()


MAX_DEPLOYMENT_RETRIES = 15


class DeploymentStatus(Enum):
    IN_PROGRESS = "InProgress"
    FAILURE = "Failure"
    SUCCESS = "Success"


class DeploymentTrackingEvent(BaseModel):
    device_id: DeviceId
    version_tag: str
    retry_num: int = 0
    deployment_id: Optional[str]


def upgrade_controller(controller_id: ControllerId) -> str:
    try:
        greengrass_version = get_device_by_controller_id(ControllerId(controller_id)).get("greengrassVersion", 1)
        _, deployment_id = controller_manger_factory.get(greengrass_version).upgrade(core_name=controller_id)
        logger.info(f"Successfully asked for upgrading controller {controller_id}. deployment_id: {deployment_id}")
        return deployment_id
    except Exception as e:
        logger.exception(f"Upgrading controller {controller_id} failed. error: {e}")
        raise e


DEFAULT_MINUTES_TO_NEXT_CHECK = 30

def schedule_deployment_tracking(deployment_id, device_id, version_tag, retry_num: int, minutes_to_next_check=DEFAULT_MINUTES_TO_NEXT_CHECK):
    next_check_time = (datetime.now() + timedelta(minutes=minutes_to_next_check)).isoformat(timespec='seconds')
    scheduler_client.create_schedule(
        ClientToken=f'{device_id}_{version_tag}',
        FlexibleTimeWindow={"Mode": "OFF"},
        Name=f'{os.environ["ENV"]}-device_{device_id}-version_upgrade_{int(datetime.now().timestamp())}',
        ScheduleExpression=f'at({next_check_time})',
        Target={
            "Arn": parameters.get_queue_arn("device-deployment-tracker"),
            "Input": json.dumps({
                "device_id": device_id,
                "version_tag": version_tag,
                "deployment_id": deployment_id,
                "retry_num": retry_num
            }),
            "RoleArn": os.environ["scheduler_role"]
        }
    )
    logger.info(f"Successfully scheduled deployment tracking for device {device_id} at {next_check_time}")


def get_deployment_status(deployment_id: str, group_id: str) -> DeploymentStatus:
    response = gg_client.get_deployment_status(
        DeploymentId=deployment_id,
        GroupId=group_id
    )
    if response.get("DeploymentStatus") == DeploymentStatus.SUCCESS.value:
        return DeploymentStatus.SUCCESS
    elif response.get("DeploymentStatus") == "Failure":
        return DeploymentStatus.FAILURE
    else:
        return DeploymentStatus.IN_PROGRESS


def handle_deployment_tracking(event):
    device = device_repository.get_device(event.device_id)

    # Case 1: initial upgrade deployment
    if event.deployment_id is None:
        deployment_id = upgrade_controller(ControllerId(device["controllerId"]))
        schedule_deployment_tracking(deployment_id, event.device_id, event.version_tag, event.retry_num + 1)
        return

    deployment_status = get_deployment_status(event.deployment_id, device["groupId"])
    if deployment_status == DeploymentStatus.SUCCESS:
        logger.info(f"Successfully upgraded device {event.device_id} to version {event.version_tag}")
    elif event.retry_num >= MAX_DEPLOYMENT_RETRIES:
        logger.info(f"Reached maximum retry number. Stop retrying.")
        monitoring_client.send_alert(f"Failed to upgrade device {event.device_id} to version {event.version_tag}. "
                                     f"Deployment status: {deployment_status}",
                                     context={"controller_id": device["controllerId"]})
    elif deployment_status == DeploymentStatus.FAILURE:
        logger.info(f"Failed to upgrade device {event.device_id} to version {event.version_tag}. trying again.")
        deployment_id = upgrade_controller(ControllerId(device["controllerId"]))
        schedule_deployment_tracking(deployment_id, event.device_id, event.version_tag, event.retry_num + 1, 60*event.retry_num)
    elif event.retry_num >= 3 and device["connectionStatus"] == ConnectionStatus.DISCONNECTED.value:
        logger.info(f"Device {event.device_id} is disconnected. Will not retry upgrading it.")
    elif deployment_status == DeploymentStatus.IN_PROGRESS and event.retry_num > 5:
        logger.info(f"Deployment is still in progress after 5 retries. deploying again and checking again in 2 hours.")
        deployment_id = upgrade_controller(ControllerId(device["controllerId"]))
        schedule_deployment_tracking(deployment_id, event.device_id, event.version_tag, event.retry_num + 1, 120)
    else:
        logger.info(f"Deployment is still in progress. rechecking in 1 hour.")
        schedule_deployment_tracking(event.deployment_id, event.device_id, event.version_tag, event.retry_num + 1, 60)


def lambda_handler(event, _):
    logger.info(f"Received event: {event}")
    event = extract_event(event, DeploymentTrackingEvent)
    handle_deployment_tracking(event)
