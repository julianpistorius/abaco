import os
import sys
import threading

import channelpy
from agave import Agave

from channels import ActorMsgChannel, ClientsChannel, CommandChannel,WorkerChannel
from codes import ERROR, READY, BUSY, COMPLETE
from docker_utils import DockerError, DockerStartContainerError, execute_actor, pull_image
from errors import WorkerException
from models import Actor, Execution, Worker
from stores import actors_store, workers_store

from agaveflask.logs import get_logger
logger = get_logger(__name__)


# keep_running will be updated by the thread listening on the worker channel when a graceful shutdown is
# required.
keep_running = True

def shutdown_worker(ch_name):
    """Gracefully shutdown a single worker."""
    logger.debug("shutdown_worker called for ch_name: {}".format(ch_name))
    ch = WorkerChannel(name=ch_name)
    ch.put("stop")
    logger.info("A 'stop' message was sent to worker channel: {}".format(ch_name))

def shutdown_workers(actor_id):
    """Graceful shutdown of all workers for an actor. Pass db_id as the `actor_id` argument."""
    logger.debug("shutdown_workers() called for actor: {}".format(actor_id))
    workers = Worker.get_workers(actor_id)
    # @TODO - this code is not thread safe. we need to update the workers state in a transaction:
    for _, worker in workers.items():
        # if there is already a channel, tell the worker to shut itself down
        if 'ch_name' in worker:
            logger.info("worker had channel: {}. calling shutdown_worker() for worker: {}".format(worker['ch_name'],
                                                                                                  worker.get('id')))
            shutdown_worker(worker['ch_name'])
        else:
            # otherwise, just remove from db:
            try:
                logger.info("worker: {} did not have a channel. Deleting worker.".format(worker.get('id')))
                worker_id = worker.get('id')
                Worker.delete_worker(actor_id, worker_id)
            except WorkerException as e:
                logger.error("Got a WorkerException trying to delete worker with id: {}; exception: {}".format(
                    worker_id, e))

def process_worker_ch(tenant, worker_ch, actor_id, worker_id, actor_ch, ag_client):
    """ Target for a thread to listen on the worker channel for a message to stop processing.
    :param worker_ch:
    :return:
    """
    global keep_running
    logger.info("Worker subscribing to worker channel...")
    while True:
        try:
            msg = worker_ch.get(timeout=2)
        except channelpy.ChannelTimeoutException:
            continue
        logger.debug("Received message in worker channel: {}".format(msg))
        logger.debug("Type(msg)={}".format(type(msg)))
        if type(msg) == dict:
            value = msg.get('value', '')
            if value == 'status':
                # this is a health check, return 'ok' to the reply_to channel.
                logger.debug("received health check. returning 'ok'.")
                ch = msg['reply_to']
                ch.put('ok')
        elif msg == 'stop':
            logger.info("Received stop message, stopping worker...")
            # first, delete an associated client
            # its possible this worker was not passed a client,
            # but if so, we need to delete it before shutting down.
            if ag_client:
                logger.info("Requesting client {} be deleted.".format(ag_client.api_key))
                secret = os.environ.get('_abaco_secret')
                clients_ch = ClientsChannel()
                msg = clients_ch.request_delete_client(tenant=tenant,
                                                       actor_id=actor_id,
                                                       worker_id=worker_id,
                                                       client_id=ag_client.api_key,
                                                       secret=secret)

                if msg['status'] == 'ok':
                    logger.info("Delete request completed successfully.")
                else:
                    logger.error("Error deleting client. Message: {}".format(msg['message']))
            else:
                logger.info("Did not receive client. Not issuing delete. Exiting.")
            try:
                Worker.delete_worker(actor_id, worker_id)
            except WorkerException as e:
                logger.info("Got WorkerException from delete_worker(). Exception: {}".format(e))
            keep_running = False
            actor_ch.close()
            logger.info("Closing actor channel for actor: {}".format(actor_id))
            logger.info("Worker is now exiting.")
            sys.exit()

def subscribe(tenant,
              actor_id,
              worker_id,
              api_server,
              client_id,
              client_secret,
              access_token,
              refresh_token,
              worker_ch):
    """
    Main loop for the Actor executor worker. Subscribes to the actor's inbox and executes actor
    containers when message arrive. Also subscribes to the worker channel for future communications.
    :return:
    """
    logger.debug("Top of subscribe().")
    actor_ch = ActorMsgChannel(actor_id)
    ag = None
    if api_server and client_id and client_secret and access_token and refresh_token:
        logger.info("Creating agave client.")
        ag = Agave(api_server=api_server,
                   token=access_token,
                   refresh_token=refresh_token,
                   api_key=client_id,
                   api_secret=client_secret)
    else:
        logger.info("Not creating agave client.")
    logger.info("Starting the process worker channel thread.")
    t = threading.Thread(target=process_worker_ch, args=(tenant, worker_ch, actor_id, worker_id, actor_ch, ag))
    t.start()
    logger.info("Worker subscribing to actor channel.")
    update_worker_status = True
    global keep_running
    while keep_running:
        if update_worker_status:
            Worker.update_worker_status(actor_id, worker_id, READY)
            update_worker_status = False
        try:
            msg = actor_ch.get(timeout=2)
        except channelpy.ChannelTimeoutException:
            continue
        except channelpy.ChannelClosedException:
            logger.info("Channel closed, worker exiting...")
            keep_running = False
            sys.exit()
        update_worker_status = True
        logger.info("Received message {}. Starting actor container...".format(msg))
        # the msg object is a dictionary with an entry called message and an arbitrary
        # set of k:v pairs coming in from the query parameters.
        message = msg.pop('message', '')
        actor = Actor.from_db(actors_store[actor_id])
        execution_id = msg['_abaco_execution_id']

        # the execution object was created by the controller, but we need to add the worker id to it now that we
        # know which worker will be working on the execution.
        logger.debug("Adding worker_id to execution.")
        Execution.add_worker_id(actor_id, execution_id, worker_id)

        # privileged dictates whether the actor container runs in privileged mode and if docker daemon is mounted.
        privileged = False
        if actor['privileged'] == 'TRUE':
            privileged = True
        logger.debug("privileged: {}".format(privileged))

        # retrieve the default environment registered with the actor.
        environment = actor['default_environment']
        logger.debug("Actor default environment: {}".format(environment))

        # overlay the default_environment registered for the actor with the msg
        # dictionary
        environment.update(msg)
        environment['_abaco_access_token'] = ''
        environment['_abaco_actor_dbid'] = actor_id
        environment['_abaco_actor_id'] = actor.id
        environment['_abaco_actor_state'] = actor.state
        logger.debug("Overlayed environment: {}".format(environment))

        # if we have an agave client, get a fresh set of tokens:
        if ag:
            try:
                ag.token.refresh()
                token = ag.token.token_info['access_token']
                environment['_abaco_access_token'] = token
                logger.info("Refreshed the tokens. Passed {} to the environment.".format(token))
            except Exception as e:
                logger.error("Got an exception trying to get an access token: {}".format(e))
        else:
            logger.info("Agave client `ag` is None -- not passing access token.")
        logger.info("Passing update environment: {}".format(environment))
        try:
            stats, logs, final_state, exit_code = execute_actor(actor_id, worker_id, worker_ch, image,
                                                                message, environment, privileged)
        except DockerStartContainerError as e:
            logger.error("Got DockerStartContainerError: {}".format(str(e)))
            Actor.set_status(actor_id, ERROR)
            continue
        # Add the completed stats to the execution
        logger.info("Actor container finished successfully. Got stats object:{}".format(str(stats)))
        Execution.finalize_execution(actor_id, execution_id, COMPLETE, stats, final_state, exit_code)
        logger.info("Added execution: {}".format(execution_id))

        # Add the logs to the execution
        Execution.set_logs(execution_id, logs)
        logger.info("Added execution logs.")

        # Update the worker's last updated and last execution fields:
        Worker.update_worker_execution_time(actor_id, worker_id)
        logger.info("worker time stamps updated.")

def main(worker_ch_name, worker_id, image):
    """
    Main function for the worker process.

    This function
    """
    logger.info("Entering main() for worker: {}, channel: {}, image: {}".format(
        worker_id, worker_ch_name, image))
    worker_ch = WorkerChannel(name=worker_ch_name)

    # first, attempt to pull image from docker hub:
    try:
        logger.info("Worker pulling image {}...".format(image))
        pull_image(image)
    except DockerError as e:
        # return a message to the spawner that there was an error pulling image and abort
        # this is not necessarily an error state: the user simply could have provided an
        # image name that does not exist in the registry. This is the first time we would
        # find that out.
        logger.info("worker got a DockerError trying to pull image. Error: {}.".format(e))
        worker_ch.put({'status': 'error', 'msg': str(e)})
        raise e
    logger.info("Image {} pulled successfully.".format(image))

    # inform spawner that image pulled successfully and, simultaneously,
    # wait to receive message from spawner that it is time to subscribe to the actor channel
    logger.debug("Worker waiting on message from spawner...")
    result = worker_ch.put_sync({'status': 'ok'})
    logger.info("Worker received reply from spawner. result: {}.".format(result))

    if result['status'] == 'error':
        # we do not expect to get an error response at this point. this needs investigation
        logger.error("Worker received error message from spawner: {}. Quiting...".format(str(result)))
        raise WorkerException(str(result))

    actor_id = result.get('actor_id')
    tenant = result.get('tenant')
    logger.info("Worker received ok from spawner. Message: {}, actor_id:{}".format(result, actor_id))
    api_server = None
    client_id = None
    client_secret = None
    access_token = None
    refresh_token = None
    if result.get('client') == 'yes':
        logger.info("Got client: yes, result: {}".format(result))
        api_server = result.get('api_server')
        client_id = result.get('client_id')
        client_secret = result.get('client_secret')
        access_token = result.get('access_token')
        refresh_token = result.get('refresh_token')
    else:
        logger.info("Did not get client:yes, got result:{}".format(result))
    Actor.set_status(actor_id, READY)
    logger.info("Actor status set to READY. subscribing to inbox.")
    subscribe(tenant,
              actor_id,
              worker_id,
              api_server,
              client_id,
              client_secret,
              access_token,
              refresh_token,
              worker_ch)


if __name__ == '__main__':
    # This is the entry point for the worker container.
    # Worker containers are launched by spawners and spawners pass the initial configuration
    # data for the worker through environment variables.
    logger.info("Inital log for new worker.")

    # read channel, worker_id and image from the environment
    worker_ch_name = os.environ.get('ch_name')
    worker_id = os.environ.get('worker_id')
    image = os.environ.get('image')
    logger.info("Channel name: {}  Image: {} ".format(worker_ch_name, image))

    # call the main() function:
    main(worker_ch_name, worker_id, image)
