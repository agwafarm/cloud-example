from aws_cdk import Stack, aws_ssm as ssm
from cdk_common.constructs.general_lambda import GeneralLambda, LambdaQueueTrigger
from cdk_common.objects_names import stack_name, ENV, ssm_param_name, role_name
from cdk_common.roles import general_lambda_role, general_scheduler_role
from constructs import Construct


class DevopsStack(Stack):
    def __init__(self,
                 scope: Construct,
                 **kwargs):
        super().__init__(scope, id=stack_name("devops-service"), **kwargs)

        self.scheduler = general_scheduler_role(self, role_name("version_upgrade_scheduler"))

        self.device_version_upgrader = GeneralLambda(self,
                                                     name="devices-version-upgrader",
                                                     environment=ENV,
                                                     code_rel_path="devops/devices_version_upgrader",
                                                     role_invokable=general_lambda_role,
                                                     environment_variables={
                                                         "scheduler_role": self.scheduler.role_arn},
                                                     concurrency=3,
                                                     dockerized=False)

        self.device_deployment_tracker = GeneralLambda(self,
                                                       name="device-deployment-tracker",
                                                       environment=ENV,
                                                       code_rel_path="devops/device_deployment_tracker",
                                                       role_invokable=general_lambda_role,
                                                       environment_variables={
                                                           "scheduler_role": self.scheduler.role_arn},
                                                       trigger=LambdaQueueTrigger(),
                                                       concurrency=3,
                                                       dockerized=False)

        devices_to_skip_param = ssm_param_name("device-deployment/devices_to_skip")
        ssm.StringParameter(self,
                            id=devices_to_skip_param,
                            parameter_name=devices_to_skip_param,
                            string_value='[]')
