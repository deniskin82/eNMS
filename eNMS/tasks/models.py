from apscheduler.jobstores.base import JobLookupError
from datetime import datetime, timedelta
from sqlalchemy import Column, ForeignKey, Integer, String, PickleType
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from time import sleep

from eNMS import db, scheduler
from eNMS.base.associations import task_log_rule_table
from eNMS.base.custom_base import CustomBase
from eNMS.base.helpers import retrieve
from eNMS.base.properties import cls_to_properties


def scheduler_job(task_name):
    with scheduler.app.app_context():
        task = retrieve(Task, name=task_name)
        workflow = task.job if task.type == 'WorkflowTask' else None
        task.run(workflow)


class Task(CustomBase):

    __tablename__ = 'Task'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    creation_time = Column(String)
    status = Column(String)
    type = Column(String)
    user_id = Column(Integer, ForeignKey('User.id'))
    user = relationship('User', back_populates='tasks')
    frequency = Column(String)
    start_date = Column(String)
    end_date = Column(String)
    positions = Column(MutableDict.as_mutable(PickleType), default={})
    job_id = Column(Integer, ForeignKey('Job.id'))
    job = relationship('Job', back_populates='scheduled_tasks')
    waiting_time = Column(Integer, default=0)
    log_rules = relationship(
        'LogRule',
        secondary=task_log_rule_table,
        back_populates='tasks'
    )

    __mapper_args__ = {
        'polymorphic_identity': 'Task',
        'polymorphic_on': type
    }

    def __init__(self, **kwargs):
        self.update(**kwargs)
        self.status = 'Active'
        self.creation_time = str(datetime.now())
        self.is_active = True
        schedule_task = kwargs['start-task']
        if schedule_task != 'do-not-run':
            self.schedule(schedule_task == 'run-now')

    def aps_conversion(self, date):
        dt = datetime.strptime(date, '%d/%m/%Y %H:%M:%S')
        return datetime.strftime(dt, '%Y-%m-%d %H:%M:%S')

    def aps_date(self, datetype):
        date = getattr(self, datetype)
        return self.aps_conversion(date) if date else None

    def pause_task(self):
        scheduler.pause_job(self.creation_time)
        self.status = 'Suspended'
        db.session.commit()

    def resume_task(self):
        scheduler.resume_job(self.creation_time)
        self.status = 'Active'
        db.session.commit()

    def delete_task(self):
        try:
            scheduler.delete_job(self.creation_time)
        except JobLookupError:
            pass
        db.session.commit()

    def task_sources(self, workflow, type='all'):
        return [
            x.source for x in self.sources
            if (type == 'all' or x.type == type) and x.workflow == workflow
        ]

    def task_successors(self, workflow, type='all'):
        return [
            x.destination for x in self.destinations
            if (type == 'all' or x.type == type) and x.workflow == workflow
        ]

    def schedule(self, run_now=True):
        now = datetime.now() + timedelta(seconds=15)
        runtime = now if run_now else self.aps_date('start_date')
        if self.frequency:
            scheduler.add_job(
                id=self.creation_time,
                func=scheduler_job,
                args=[self.name],
                trigger='interval',
                start_date=runtime,
                end_date=self.aps_date('end_date'),
                seconds=int(self.frequency),
                replace_existing=True
            )
        else:
            scheduler.add_job(
                id=self.creation_time,
                func=scheduler_job,
                run_date=runtime,
                args=[self.name],
                trigger='date',
                replace_existing=True
            )
        return runtime

    @property
    def properties(self):
        return {p: getattr(self, p) for p in cls_to_properties['Task']}


class ServiceTask(Task):

    __tablename__ = 'ServiceTask'

    id = Column(Integer, ForeignKey('Task.id'), primary_key=True)

    __mapper_args__ = {
        'polymorphic_identity': 'ServiceTask',
    }

    def __init__(self, **data):
        self.job = data.pop('job')
        super().__init__(**data)

    def run(self, workflow, workflow_results=None):
        try:
            results = self.job.job(self, workflow_results)
        except Exception as e:
            return {'success': False, 'result': str(e)}
        self.job.logs[str(datetime.now())] = results
        db.session.commit()
        return results

    @property
    def serialized(self):
        properties = self.properties
        properties['job'] = self.job.properties if self.job else None
        return properties


class WorkflowTask(Task):

    __tablename__ = 'WorkflowTask'

    id = Column(Integer, ForeignKey('Task.id'), primary_key=True)

    __mapper_args__ = {
        'polymorphic_identity': 'WorkflowTask',
    }

    def __init__(self, **data):
        self.job = data.pop('job')
        super().__init__(**data)

    def run(self, workflow=None):
        runtime = str(datetime.now())
        start_task = retrieve(Task, id=self.job.start_task)
        if not start_task:
            return False, {runtime: 'No start task in the workflow.'}
        tasks, visited = [start_task], set()
        workflow_results = {}
        while tasks:
            task = tasks.pop()
            # We check that all predecessors of the task have been visited
            # to ensure that the task will receive the full payload.
            # If it isn't the case, we put it back in the heap and move on to
            # another task.
            if any(n not in visited for n in task.task_sources(self.job)):
                continue
            visited.add(task)
            task_results = task.run(workflow, workflow_results)
            success = task_results['success']
            if task.id == self.job.end_task:
                workflow_results['success'] = success
            for successor in task.task_successors(self.job, success):
                if successor not in visited:
                    tasks.append(successor)
            workflow_results[task.name] = task_results
            sleep(task.waiting_time)
        self.job.logs[runtime] = workflow_results
        db.session.commit()
        return workflow_results

    @property
    def serialized(self):
        properties = {p: getattr(self, p) for p in cls_to_properties['Task']}
        properties['job'] = self.job.properties if self.job else None
        return properties
