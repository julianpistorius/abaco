from copy import deepcopy
import json
import time
import uuid

from agaveflask.utils import RequestParser

from channels import CommandChannel
from codes import REQUESTED, SUBMITTED
from config import Config
import errors

from stores import actors_store, clients_store, executions_store, logs_store, permissions_store, workers_store

from agaveflask.logs import get_logger
logger = get_logger(__name__)


def under_to_camel(value):
    def camel_case():
        yield type(value).lower
        while True:
            yield type(value).capitalize
    c = camel_case()
    return "".join(c.__next__()(x) if x else '_' for x in value.split("_"))


def dict_to_camel(d):
    """Convert all keys in a dictionary to camel case."""
    d2 = {}
    for k,v in d.items():
        d2[under_to_camel(k)] = v
    return d2


class DbDict(dict):
    """Class for persisting a Python dictionary."""

    def __getattr__(self, key):
        # returning an AttributeError is important for making deepcopy work. cf.,
        # http://stackoverflow.com/questions/25977996/supporting-the-deep-copy-operation-on-a-custom-class
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(e)

    def __setattr__(self, key, value):
        self[key] = value

    def to_db(self):
        # json serialization now happening in the store class. @todo - should remove all uses of .to_db
        return self
        # return json.dumps(self)


class AbacoDAO(DbDict):
    """Base Data Access Object class for Abaco models."""

    # the parameters for the DAO
    # tuples of the form (param name, required/optional/provided/derived, attr_name, type, help, default)
    # should be defined in the subclass.
    #
    #
    # required: these fields are required in the post/put methods of the web app.
    # optional: these fields are optional in the post/put methods of the web app and have a default value.
    # provided: these fields are required to construct the DAO but are provided by the abaco client code, not the user
    #           and not the DAO class code.
    # derived: these fields are derived by the DAO class code and do not need to be passed.
    PARAMS = []

    @classmethod
    def request_parser(cls):
        """Return a flask RequestParser object that can be used in post/put processing."""
        parser = RequestParser()
        for name, source, attr, typ, help, default in cls.PARAMS:
            if source == 'derived':
                continue
            required = source == 'required'
            parser.add_argument(name, type=typ, required=required, help=help, default=default)
        return parser

    @classmethod
    def from_db(cls, db_json):
        """Construct a DAO from a db serialization."""
        return cls(**db_json)

    def __init__(self, **kwargs):
        """Construct a DAO from **kwargs. Client can also create from a dictionary, d, using AbacoDAO(**d)"""
        for name, source, attr, typ, help, default in self.PARAMS:
            if source == 'required':
                try:
                    value = kwargs[name]
                except KeyError:
                    logger.debug("required missing field: {}. ".format(name))
                    raise errors.DAOError("Required field {} missing.".format(name))
            elif source == 'optional':
                value = kwargs.get(name, default)
            elif source == 'provided':
                try:
                    value = kwargs[name]
                except KeyError:
                    logger.debug("provided field missing: {}.".format(name))
                    raise errors.DAOError("Required field {} missing.".format(name))
            else:
                # derived value - check to see if already computed
                if hasattr(self, name):
                    value = getattr(self, name)
                else:
                    value = self.get_derived_value(name, kwargs)
            setattr(self, attr, value)

    def get_uuid(self):
        """Generate a random uuid."""
        return '{}-{}'.format(str(uuid.uuid1()), self.get_uuid_code())

    def get_derived_value(self, name, d):
        """Compute a derived value for the attribute `name` from the dictionary d of attributes provided."""
        raise NotImplementedError

    def case(self):
        """Convert to camel case, if required."""
        case = Config.get('web', 'case')
        if not case == 'camel':
            return self
        # if camel case, convert all attributes
        for name, _, _, _, _, _ in self.PARAMS:
            val = self.pop(name, None)
            if val is not None:
                self.__setattr__(under_to_camel(name), val)
        return self

    def display(self):
        """A default display method, for those subclasses that do not define their own."""
        return self.case()


class Actor(AbacoDAO):
    """Basic data access object for working with Actors."""

    PARAMS = [
        # param_name, required/optional/provided/derived, attr_name, type, help, default
        ('name', 'required', 'name', str, 'User defined name for this actor.', None),
        ('image', 'required', 'image', str, 'Reference to image on docker hub for this actor.', None),

        ('stateless', 'optional', 'stateless', bool, 'Whether the actor stores private state.', False),
        ('description', 'optional', 'description', str,  'Description of this actor', ''),
        ('privileged', 'optional', 'privileged', bool, 'Whether this actor runs in privileged mode.', False),
        ('default_environment', 'optional', 'default_environment', dict, 'Default environmental variables and values.', {}),
        ('status', 'optional', 'status', str, 'Current status of the actor.', SUBMITTED),
        ('status_message', 'optional', 'status_message', str, 'Explanation of status.', ''),
        ('executions', 'optional', 'executions', dict, 'Executions for this actor.', {}),
        ('state', 'optional', 'state', dict, "Current state for this actor.", {}),

        ('tenant', 'provided', 'tenant', str, 'The tenant that this actor belongs to.', None),
        ('api_server', 'provided', 'api_server', str, 'The base URL for the tenant that this actor belongs to.', None),
        ('owner', 'provided', 'owner', str, 'The user who created this actor.', None),

        ('db_id', 'derived', 'db_id', str, 'Primary key in the database for this actor.', None),
        ('id', 'derived', 'id', str, 'Human readable id for this actor.', None),
        ]

    def get_derived_value(self, name, d):
        """Compute a derived value for the attribute `name` from the dictionary d of attributes provided."""
        # first, see if the attribute is already in the object:
        if hasattr(self, 'id'):
            return
        # next, see if it was passed:
        try:
            return d[name]
        except KeyError:
            pass
        # if not, generate an id
        try:
            actor_id, db_id = self.generate_id(d['name'], d['tenant'])
        except KeyError:
            logger.debug("name or tenant missing from actor dict: {}.".format(d))
            raise errors.DAOError("Required field name or tenant missing")
        self.id = actor_id
        self.db_id = db_id
        if name == 'id':
            return actor_id
        else:
            return db_id

    def get_uuid_code(self):
        """ Return the Agave code for this object.
        :return: str
        """
        return '059'

    def display(self):
        """Return a representation fit for display."""
        self.update(self.get_hypermedia())
        self.pop('db_id')
        self.pop('executions')
        self.pop('tenant')
        self.pop('api_server')
        return self.case()

    def get_hypermedia(self):
        return {'_links': { 'self': '{}/actors/v2/{}'.format(self.api_server, self.id),
                            'owner': '{}/profiles/v2/{}'.format(self.api_server, self.owner),
                            'executions': '{}/actors/v2/{}/executions'.format(self.api_server, self.id)
        }}

    def generate_id(self, name, tenant):
        """Generate an id for a new actor."""
        id = self.get_uuid()
        return id, Actor.get_dbid(tenant, id)

    def ensure_one_worker(self):
        """This method will check the workers store for the actor and request a new worker if none exist."""
        logger.debug("top of Actor.ensure_one_worker().")
        worker_id = Worker.ensure_one_worker(self.db_id)
        logger.debug("Worker.ensure_one_worker returned worker_id: {}".format(worker_id))
        if worker_id:
            worker_ids = [worker_id]
            logger.info("Actor.ensure_one_worker() putting message on command channel for worker_id: {}".format(
                worker_id))
            ch = CommandChannel()
            ch.put_cmd(actor_id=self.db_id,
                       worker_ids=worker_ids,
                       image=self.image,
                       tenant=self.tenant,
                       num=1,
                       stop_existing=False)
            return worker_ids
        else:
            logger.debug("Actor.ensure_one_worker() returning None.")
            return None

    @classmethod
    def get_dbid(cls, tenant, id):
        """Return the key used in redis from the "display_id" and tenant. """
        return str('{}_{}'.format(tenant, id))

    @classmethod
    def get_display_id(cls, tenant, dbid):
        """Return the display id from the dbid."""
        if tenant + '_' in dbid:
            return dbid[len(tenant + '_'):]
        else:
            return dbid

    @classmethod
    def set_status(cls, actor_id, status, status_message=None):
        """Update the status of an actor"""
        logger.debug("top of set_status for status: {}".format(status))
        actors_store.update(actor_id, 'status', status)
        if status_message:
            actors_store.update(actor_id, 'status_message', status_message)


class Execution(AbacoDAO):
    """Basic data access object for working with actor executions."""

    PARAMS = [
        # param_name, required/optional/provided/derived, attr_name, type, help, default
        ('tenant', 'required', 'tenant', str, 'The tenant that this execution belongs to.', None),
        ('api_server', 'required', 'api_server', str, 'The base URL for the tenant that this actor belongs to.', None),
        ('actor_id', 'required', 'actor_id', str, 'The human readable id for the actor associated with this execution.', None),
        ('executor', 'required', 'executor', str, 'The user who triggered this execution.', None),
        ('worker_id', 'optional', 'worker_id', str, 'The worker who supervised this execution.', None),
        ('runtime', 'required', 'runtime', str, 'Runtime, in milliseconds, of the execution.', None),
        ('cpu', 'required', 'cpu', str, 'CPU usage, in user jiffies, of the execution.', None),
        ('io', 'required', 'io', str,
         'Block I/O usage, in number of 512-byte sectors read from and written to, by the execution.', None),
        ('id', 'derived', 'id', str, 'Human readable id for this execution.', None),
        ('status', 'required', 'status', str, 'Status of the execution.', None),\
        ('exit_code', 'optional', 'exit_code', str, 'The exit code of this execution.', None),
        ('final_state', 'optional', 'final_state', str, 'The final state of the execution.', None),
    ]

    def get_derived_value(self, name, d):
        """Compute a derived value for the attribute `name` from the dictionary d of attributes provided."""
        # first, see if the attribute is already in the object:
        try:
            if d[name]:
                return d[name]
        except KeyError:
            pass
        return self.get_uuid()

    @classmethod
    def add_execution(cls, actor_id, ex):
        """
        Add an execution to an actor.
        :param actor_id: str; the dbid of the actor
        :param ex: dict describing the execution.
        :return:
        """
        logger.debug("top of add_execution for actor: {} and execution: {}.".format(actor_id, ex))
        actor = Actor.from_db(actors_store[actor_id])
        ex.update({'actor_id': actor_id,
                   'tenant': actor.tenant,
                   'api_server': actor['api_server']
                   })
        execution = Execution(**ex)

        try:
            executions_store.update(actor_id, execution.id, execution)
        except KeyError:
            # if actor has no executions, a KeyError will be thrown
            executions_store[actor_id] = {execution.id: execution}
        logger.info("Execution: {} saved for actor: {}.".format(ex, actor_id))
        return execution.id

    @classmethod
    def add_worker_id(cls, actor_id, execution_id, worker_id):
        """
        :param actor_id: the id of the actor
        :param execution_id: the id of the execution
        :param worker_id: the id of the worker that executed this actor.
        :return:
        """
        logger.debug("top of add_worker_id() for actor: {} execution: {} worker: {}".format(
            actor_id, execution_id, worker_id))
        try:
            executions_store.update_subfield(actor_id, execution_id, 'worker_id', worker_id)
            logger.debug("worker added to execution: {} actor: {} worker: {}".format(
            execution_id, actor_id, worker_id))
        except KeyError as e:
            logger.error("Could not add an execution. KeyError: {}. actor: {}. ex: {}. worker: {}".format(
                e, actor_id, execution_id, worker_id))
            raise errors.ExecutionException("Execution {} not found.".format(execution_id))

    @classmethod
    def finalize_execution(cls, actor_id, execution_id, status, stats, final_state, exit_code):
        """
        Update an execution status and stats after the execution is complete or killed.
         `actor_id` should be the dbid of the actor.
         `execution_id` should be the id of the execution returned from a prior call to add_execution.
         `status` should be the final status of the execution.
         `stats` parameter should be a dictionary with io, cpu, and runtime.
         `final_state` parameter should be the `State` object returned from the docker inspect command.
         `exit_code` parameter should be the exit code of the container.
         """
        params_str = "actor: {}. ex: {}. status: {}. final_state: {}. exit_code: {}. stats: {}".format(
            actor_id, execution_id, status, final_state, exit_code, stats)
        logger.debug("top of finalize_execution. Params: {}".format(params_str))
        if not 'io' in stats:
            logger.error("Could not finalize execution. io missing. Params: {}".format(params_str))
            raise errors.ExecutionException("'io' parameter required to finalize execution.")
        if not 'cpu' in stats:
            logger.error("Could not finalize execution. cpu missing. Params: {}".format(params_str))
            raise errors.ExecutionException("'cpu' parameter required to finalize execution.")
        if not 'runtime' in stats:
            logger.error("Could not finalize execution. runtime missing. Params: {}".format(params_str))
            raise errors.ExecutionException("'runtime' parameter required to finalize execution.")
        try:
            executions_store.update_subfield(actor_id, execution_id, 'status', status)
            executions_store.update_subfield(actor_id, execution_id, 'io', stats['io'])
            executions_store.update_subfield(actor_id, execution_id, 'cpu', stats['cpu'])
            executions_store.update_subfield(actor_id, execution_id, 'runtime', stats['runtime'])
            executions_store.update_subfield(actor_id, execution_id, 'final_state', final_state)
            executions_store.update_subfield(actor_id, execution_id, 'exit_code', exit_code)
        except KeyError:
            logger.error("Could not finalize execution. execution not found. Params: {}".format(params_str))
            raise errors.ExecutionException("Execution {} not found.".format(execution_id))

    @classmethod
    def set_logs(cls, exc_id, logs):
        """
        Set the logs for an execution.
        :param exc_id: the id of the execution (str)
        :param logs: dict describing the logs
        :return:
        """
        log_ex = Config.get('web', 'log_ex')
        try:
            log_ex = int(log_ex)
        except ValueError:
            log_ex = -1
        if log_ex > 0:
            logger.info("Storing log with expiry. exc_id: {}".format(exc_id))
            logs_store.set_with_expiry(exc_id, logs)
        else:
            logger.info("Storing log without expiry. exc_id: {}".format(exc_id))
            logs_store[exc_id] = logs

    def get_uuid_code(self):
        """ Return the Agave code for this object.
        :return: str
        """
        return '053'

    def get_hypermedia(self):
        return {'_links': { 'self': '{}/actors/v2/{}/executions/{}'.format(self.api_server, self.actor_id, self.id),
                            'owner': '{}/profiles/v2/{}'.format(self.api_server, self.executor),
                            'logs': '{}/actors/v2/{}/executions/{}/logs'.format(self.api_server, self.actor_id, self.id)
        }}

    def display(self):
        """Return a display version of the execution."""
        self.update(self.get_hypermedia())
        tenant = self.pop('tenant')
        self.actor_id = Actor.get_display_id(tenant, self.actor_id)
        return self.case()


class ExecutionsSummary(AbacoDAO):
    """ Summary information for all executions performed by an actor. """
    PARAMS = [
        # param_name, required/optional/provided/derived, attr_name, type, help, default
        ('db_id', 'required', 'db_id', str, 'Primary key in the database for associated actor.', None),
        ('api_server', 'derived', 'api_server', str, 'Base URL for the tenant that associated actor belongs to.', None),
        ('actor_id', 'derived', 'actor_id', str, 'id for the actor.', None),
        ('owner', 'provided', 'owner', str, 'The user who created the associated actor.', None),
        ('ids', 'derived', 'ids', list, 'List of all execution ids.', None),
        ('total_executions', 'derived', 'total_executions', str, 'Total number of execution.', None),
        ('total_io', 'derived', 'total_io', str,
         'Block I/O usage, in number of 512-byte sectors read from and written to, by all executions.', None),
        ('total_runtime', 'derived', 'total_runtime', str, 'Runtime, in milliseconds, of all executions.', None),
        ('total_cpu', 'derived', 'total_cpu', str, 'CPU usage, in user jiffies, of all execution.', None),
        ]

    def compute_summary_stats(self, dbid):
        try:
            actor = actors_store[dbid]
        except KeyError:
            raise errors.DAOError(
                "actor not found: {}'".format(dbid), 404)
        tot = {'api_server': actor['api_server'],
               'actor_id': actor['id'],
               'owner': actor['owner'],
               'total_executions': 0,
               'total_cpu': 0,
               'total_io': 0,
               'total_runtime': 0,
               'ids': []}
        try:
            executions = executions_store[dbid]
        except KeyError:
            executions = {}
        for id, val in executions.items():
            tot['ids'].append(id)
            tot['total_executions'] += 1
            tot['total_cpu'] += int(val['cpu'])
            tot['total_io'] += int(val['io'])
            tot['total_runtime'] += int(val['runtime'])
        return tot

    def get_derived_value(self, name, d):
        """Compute a derived value for the attribute `name` from the dictionary d of attributes provided."""
        # first, see if the attribute is already in the object:
        try:
            if d[name]:
                return d[name]
        except KeyError:
            pass
        # if not, compute and store all values, returning the one requested:
        try:
            dbid = d['db_id']
        except KeyError:
            logger.error("db_id missing from call to get_derived_value. d: {}".format(d))
            raise errors.ExecutionException('db_id is required.')
        tot = self.compute_summary_stats(dbid)
        d.update(tot)
        return tot[name]

    def get_hypermedia(self):
        return {'_links': {'self': '{}/actors/v2/{}/executions'.format(self.api_server, self.actor_id),
                           'owner': '{}/profiles/v2/{}'.format(self.api_server, self.owner),
        }}

    def display(self):
        self.update(self.get_hypermedia())
        self.pop('db_id')
        return self.case()


class Worker(AbacoDAO):
    """Basic data access object for working with Workers."""

    PARAMS = [
        # param_name, required/optional/provided/derived, attr_name, type, help, default
        ('tenant', 'required', 'tenant', str, 'The tenant that this worker belongs to.', None),
        # `id` is required by the time a client using the __init__ method for a worker object.
        # They should already have the `id` field having used request_worker first.
        ('id', 'required', 'id', str, 'The unique id for this worker.', None),
        ('ch_name', 'required', 'ch_name', str, 'The name of the associated worker chanel.', None),
        ('image', 'required', 'image', list, 'The list of images associated with this worker', None),
        ('location', 'required', 'location', str, 'The location of the docker daemon used by this worker.', None),
        ('cid', 'required', 'cid', str, 'The container ID of this worker.', None),
        ('status', 'required', 'status', str, 'Status of the worker.', None),
        ('host_id', 'required', 'host_id', str, 'id of the host where worker is running.', None),
        ('host_ip', 'required', 'host_ip', str, 'ip of the host where worker is running.', None),
        ('last_execution', 'required', 'last_execution', str, 'Last time the worker executed an actor container.', None),
        ]

    @classmethod
    def get_uuid(cls):
        """Generate a random uuid."""
        return '{}-{}'.format(str(uuid.uuid1()), '060')


    @classmethod
    def get_workers(cls, actor_id):
        """Retrieve all workers for an actor. Pass db_id as `actor_id` parameter."""
        try:
            return workers_store[actor_id]
        except KeyError:
            return {}

    @classmethod
    def get_worker(cls, actor_id, worker_id):
        """Retrieve a worker from the workers store. Pass db_id as `actor_id` parameter."""
        try:
            return workers_store[actor_id][worker_id]
        except KeyError:
            raise errors.WorkerException("Worker not found.")

    @classmethod
    def delete_worker(cls, actor_id, worker_id):
        """Deletes a worker from the worker store. Uses Redis optimistic locking to provide thread-safety since multiple
        clients could be attempting to delete workers at the same time. Pass db_id as `actor_id`
        parameter.
        """
        logger.debug("top of delete_worker().")
        try:
            wk = workers_store.pop_field(actor_id, worker_id)
            logger.info("worker deleted. actor: {}. worker: {}.".format(actor_id, worker_id))
        except KeyError as e:
            logger.info("KeyError deleting worker. actor: {}. worker: {}. exception: {}".format(actor_id, worker_id, e))
            raise errors.WorkerException("Worker not found.")

    @classmethod
    def ensure_one_worker(cls, actor_id):
        """
        Atomically ensure at least one worker exists in the database. If not, adds a worker to the database in
        requested status.
        This method returns an id for the worker if a new worker was added and otherwise returns none.
        """
        logger.debug("top of ensure_one_worker.")
        worker_id = Worker.get_uuid()
        worker = {'status': REQUESTED, 'id': worker_id}
        val = workers_store.add_if_empty(actor_id, worker_id, worker)
        if val:
            logger.info("got worker: {} from add_if_empty.".format(val))
            return worker_id
        else:
            logger.debug("did not get worker from add_if_empty.")
            return None

    @classmethod
    def request_worker(cls, actor_id):
        """
        Add a new worker to the database in requested status. This method returns an id for the worker and
        should be called before putting a message on the command queue.
        """
        logger.debug("top of request_worker().")
        worker_id = Worker.get_uuid()
        worker = {'status': REQUESTED, 'id': worker_id}
        # it's possible the actor_id is not in the workers_store yet (i.e., new actor with no workers)
        # In that case we need to catch a KeyError:
        try:
            # we know this worker_id is new since we just generated it, so we don't need to use
            # workers_store.update()
            workers_store[actor_id][worker_id] = worker
            logger.info("added additional worker with id: {} to workers_store.".format(worker_id))
        except KeyError:
            workers_store[actor_id] = {worker_id: worker}
            logger.info("added first worker with id: {} to workers_store.".format(worker_id))
        return worker_id

    @classmethod
    def add_worker(cls, actor_id, worker):
        """
        Add a running worker to an actor's collection of workers. The `worker` parameter should be a complete
        description of the worker, including the `id` field. The worker should have already been created
        in the database in 'REQUESTED' status using request_worker, and the `id` should be the same as that
        returned.
        """
        logger.debug("top of add_worker().")
        workers_store.update(actor_id, worker['id'], worker)
        logger.info("worker {} added to actor: {}".format(worker, actor_id))

    @classmethod
    def update_worker_execution_time(cls, actor_id, worker_id):
        """Pass db_id as `actor_id` parameter."""
        logger.debug("top of update_worker_execution_time().")
        now = time.time()
        workers_store.update_subfield(actor_id, worker_id, 'last_execution', now)
        workers_store.update_subfield(actor_id, worker_id, 'last_update', now)
        logger.info("worker execution time updated. worker_id: {}".format(worker_id))

    @classmethod
    def update_worker_status(cls, actor_id, worker_id, status):
        """Pass db_id as `actor_id` parameter."""
        logger.debug("top of update_worker_status().")
        workers_store.update_subfield(actor_id, worker_id, 'status', status)
        logger.info("worker status updated to: {}. worker_id: {}".format(status, worker_id))

    def get_uuid_code(self):
        """ Return the Agave code for this object.
        :return: str
        """
        return '058'


class Client(AbacoDAO):
    """
    Data access object for OAuth clients generated for workers.
    NOTE: this is not the public python API for interacting with clients. Managing clients should be
    done through the ClientsChannel/clientg actor, since client management involves managing these models but
    also objects in the third party systems (agave APIM).
    """

    PARAMS = [
        # param_name, required/optional/provided/derived, attr_name, type, help, default
        ('tenant', 'required', 'tenant', str, 'The tenant of the worker owning the client.', None),
        ('actor_id', 'required', 'actor_id', str, 'The actor id of the worker owning the client.', None),
        ('worker_id', 'required', 'worker_id', list, 'The id of the worker owning the client.', None),
        ('client_key', 'required', 'client_key', str, 'The key of the client.', None),
        ('client_name', 'required', 'client_name', str, 'The name of the client.', None),
        ('id', 'derived', 'id', str, 'Unique id in the database for this client', None)
        ]

    def get_derived_value(self, name, d):
        """Compute a derived value for the attribute `name` from the dictionary d of attributes provided."""
        # first, see if the id attribute is already in the object:
        try:
            if d[name]:
                return d[name]
        except KeyError:
            pass
        # combine the tenant_id and client_key to get the unique id
        return Client.get_client_id(d['tenant'], d['client_key'])

    @classmethod
    def get_client_id(cls, tenant, key):
        return '{}_{}'.format(tenant, key)

    @classmethod
    def get_client(cls, tenant, client_key):
        return Client(clients_store[Client.get_client_id(tenant, client_key)])

    @classmethod
    def delete_client(cls, tenant, client_key):
        del clients_store[Client.get_client_id(tenant, client_key)]

def get_permissions(actor_id):
    """ Return all permissions for an actor
    :param actor_id:
    :return:
    """
    try:
        permissions = json.loads(permissions_store[actor_id])
        return permissions
    except KeyError:
        raise errors.PermissionsException("Actor {} does not exist".format(actor_id))

def add_permission(user, actor_id, level):
    """Add a permission for a user and level to an actor."""
    logger.debug("top of add_permission().")
    try:
        permissions = get_permissions(actor_id)
    except errors.PermissionsException:
        permissions = []
    for pem in permissions:
        if pem.get('user') == 'user' and pem.get('level') == level:
            logger.debug("user: {} alreay had permission: {} for actor: {}".format(user, level, actor_id))
            return
    permissions.append({'user': user,
                        'level': level})
    logger.info("permission: {} added for user: {} and actor: {}".format(level, user, actor_id))
    permissions_store[actor_id] = json.dumps(permissions)