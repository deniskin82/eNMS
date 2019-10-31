from datetime import datetime
from flask_wtf import FlaskForm
from pathlib import Path
from re import M, sub
from sqlalchemy import Boolean, Float, ForeignKey, Integer
from wtforms import FieldList, FormField, HiddenField, StringField
from wtforms.widgets import TextArea

from eNMS.database.dialect import Column, MutableList, LargeString, SmallString
from eNMS.forms.automation import NetmikoForm
from eNMS.models.automation import ConnectionService


class DataBackupService(ConnectionService):

    __tablename__ = "data_backup_service"
    pretty_name = "Operational Data Backup"
    id = Column(Integer, ForeignKey("connection_service.id"), primary_key=True)
    enable_mode = Column(Boolean, default=True)
    config_mode = Column(Boolean, default=False)
    driver = Column(SmallString)
    use_device_driver = Column(Boolean, default=True)
    fast_cli = Column(Boolean, default=False)
    timeout = Column(Integer, default=10.0)
    global_delay_factor = Column(Float, default=1.0)
    configuration = Column(LargeString)
    operational_data = Column(LargeString)
    replacements = Column(MutableList)

    __mapper_args__ = {"polymorphic_identity": "data_backup_service"}

    def job(self, run, payload, device):
        path = Path.cwd() / "git" / "data" / device.name
        path.mkdir(parents=True, exist_ok=True)
        try:
            device.last_runtime = datetime.now()
            netmiko_connection = run.netmiko_connection(device)
            run.log("info", "Fetching Operational Data", device)
            for data in ("configuration", "operational_data"):
                commands = run.sub(getattr(self, data), locals()).splitlines()
                print("tttt"*100, commands)
                result = "".join(
                    f"{command}:\n{netmiko_connection.send_command(command)}"
                    for command in commands
                )
                for r in self.replacements:
                    result = sub(r["pattern"], r["replace_with"], result, flags=M)
                setattr(device, data, result)
                with open(path / data, "w") as file:
                    file.write(result)
            device.last_status = "Success"
            device.last_duration = (
                f"{(datetime.now() - device.last_runtime).total_seconds()}s"
            )
            device.last_update = str(device.last_runtime)
            run.generate_yaml_file(path, device)
        except Exception as e:
            device.last_status = "Failure"
            device.last_failure = str(device.last_runtime)
            run.generate_yaml_file(path, device)
            return {"success": False, "result": str(e)}
        return {"success": True}


class ReplacementForm(FlaskForm):
    pattern = StringField("Pattern")
    replace_with = StringField("Replace With")


class DataBackupForm(NetmikoForm):
    form_type = HiddenField(default="data_backup_service")
    configuration = StringField(widget=TextArea(), render_kw={"rows": 2})
    operational_data = StringField(widget=TextArea(), render_kw={"rows": 6})
    replacements = FieldList(FormField(ReplacementForm), min_entries=3)
    groups = {
        "Main Parameters": {
            "commands": ["configuration", "operational_data", "replacements"],
            "default": "expanded",
        },
        **NetmikoForm.groups,
    }
