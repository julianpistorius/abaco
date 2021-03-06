import json
import os
import time

import rabbitpy

from codes import ERROR
from config import Config
from docker_utils import DockerError, run_worker
from models import Actor, Worker
from stores import workers_store
from channels import ActorMsgChannel, ClientsChannel, CommandChannel, WorkerChannel

from agaveflask.logs import get_logger
logger = get_logger(__name__)


class SpawnerException(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


class Spawner(object):

    def __init__(self):
        self.num_workers = int(Config.get('workers', 'init_count'))
        self.secret = os.environ.get('_abaco_secret')
        self.cmd_ch = CommandChannel()

    def run(self):
        while True:
            cmd = self.cmd_ch.get()
            self.process(cmd)

    def stop_workers(self, actor_id, worker_ids):
        """Stop existing workers; used when updating an actor's image."""
        logger.debug("Top of stop_workers() for actor: {}.".format(actor_id))
        try:
            workers_dict = workers_store[actor_id]
        except KeyError:
            logger.debug("workers_store had no workers for actor: {}".format(actor_id))
            workers_dict = {}

        # if there are existing workers, we need to close the actor message channel and
        # gracefully shutdown the existing worker processes.
        if len(workers_dict.items()) > 0:
            logger.info("Found {} workers to stop.".format(len(workers_dict.items())))
            # first, close the actor msg channel to prevent any new messages from being pulled
            # by the old workers.
            actor_ch = ActorMsgChannel(actor_id)
            actor_ch.close()
            logger.info("Actor channel closed for actor: {}".format(actor_id))
            # now, send messages to workers for a graceful shutdown:
            for _, worker in workers_dict.items():
                # don't stop the new workers:
                if worker['id'] not in worker_ids:
                    ch = WorkerChannel(name=worker['ch_name'])
                    ch.put('stop')
                    logger.info("Sent 'stop' message to worker channel: {}".format(ch))
        else:
            logger.info("No workers to stop.")

    def process(self, cmd):
        """Main spawner method for processing a command from the CommandChannel."""
        logger.info("Spawner processing new command:{}".format(cmd))
        actor_id = cmd['actor_id']
        worker_ids = cmd['worker_ids']
        image = cmd['image']
        tenant = cmd['tenant']
        stop_existing = cmd.get('stop_existing', True)
        num_workers = cmd.get('num', self.num_workers)
        logger.info("command params: actor_id: {} worker_ids: {} image: {} stop_existing: {} mum_workers: {}".format(
            actor_id, worker_ids, image, tenant, stop_existing, num_workers))
        try:
            new_channels, anon_channels, new_workers = self.start_workers(actor_id,
                                                                          worker_ids,
                                                                          image,
                                                                          tenant,
                                                                          num_workers)
        except SpawnerException as e:
            # for now, start_workers will do clean up for a SpawnerException, so we just need
            # to return back to the run loop.
            logger.info("Spawner returning to main run loop.")
            return
        logger.info("Created new workers: {}".format(new_workers))

        # stop any existing workers:
        if stop_existing:
            logger.info("Stopping existing workers: {}".format(worker_ids))
            self.stop_workers(actor_id, worker_ids)

        # add workers to store first so that the records will be there when the workers go
        # to update their status
        if not stop_existing:
            # if we're not stopping the existing workers, we need to add each worker to the
            # actor's collection.
            for _, worker in new_workers.items():
                logger.info("calling add_worker for worker: {}.".format(worker))
                Worker.add_worker(actor_id, worker)
        else:
            # since we're stopping the existing workers, the actor's collection should just
            # be equal to the new_workers.
            workers_store[actor_id] = new_workers
            logger.info("workers_store set to new_workers: {}.".format(new_workers))

        # Tell new worker to subscribe to the actor channel.
        # If abaco is configured to generate clients for the workers, generate them now
        # and send new workers their clients.
        generate_clients = Config.get('workers', 'generate_clients').lower()
        logger.info("Sending messages to new workers over anonymous channels to subscribe to inbox.")
        for idx, channel in enumerate(anon_channels):
            if generate_clients == 'true':
                logger.info("Getting client for worker {}".format(idx))
                client_ch = ClientsChannel()
                client_msg = client_ch.request_client(tenant=tenant,
                                                      actor_id=actor_id,
                                                      # new_workers is a dictionary of dictionaries; list(d) creates a
                                                      # list of keys for a dictionary d. hence, the idx^th entry
                                                      # of list(ner_workers) should be the key.
                                                      worker_id=new_workers[list(new_workers)[idx]]['id'],
                                                      secret=self.secret)
                # we need to ignore errors when generating clients because it's possible it is not set up for a specific
                # tenant. we log it instead.
                if client_msg.get('status') == 'error':
                    logger.info("Error generating client: {}".format(client_msg.get('message')))
                    channel.put({'status': 'ok',
                                 'actor_id': actor_id,
                                 'tenant': tenant,
                                 'client': 'no'})
                    logger.debug("Sent OK message over anonymous worker channel.")
                # else, client was generated successfully:
                else:
                    logger.info("Got a client: {}, {}, {}".format(client_msg['client_id'],
                                                                  client_msg['access_token'],
                                                                  client_msg['refresh_token']))
                    channel.put({'status': 'ok',
                                 'actor_id': actor_id,
                                 'tenant': tenant,
                                 'client': 'yes',
                                 'client_id': client_msg['client_id'],
                                 'client_secret': client_msg['client_secret'],
                                 'access_token': client_msg['access_token'],
                                 'refresh_token': client_msg['refresh_token'],
                                 'api_server': client_msg['api_server'],
                                 })
                    logger.debug("Sent OK message AND client over anonymous worker channel.")
            else:
                logger.info("Not generating clients. Config value was: {}".format(generate_clients))
                channel.put({'status': 'ok',
                             'actor_id': actor_id,
                             'tenant': tenant,
                             'client': 'no'})
                logger.debug("Sent OK message over anonymous worker channel.")

        logger.info("Done processing command.")

    def start_workers(self, actor_id, worker_ids, image, tenant, num_workers):
        logger.info("starting {} workers. actor_id: {} image: {}".format(str(self.num_workers), actor_id, image))
        channels = []
        anon_channels = []
        workers = {}
        try:
            for i in range(num_workers):
                worker_id = worker_ids[i]
                logger.info("starting worker {} with id: {}".format(i, worker_id))
                ch, anon_ch, worker = self.start_worker(image, tenant, worker_id)
                logger.debug("channel for worker {} is: {}".format(str(i), ch.name))
                channels.append(ch)
                anon_channels.append(anon_ch)
                workers[worker_id] = worker
        except SpawnerException as e:
            logger.info("Caught SpawnerException:{}".format(str(e)))
            # in case of an error, put the actor in error state and kill all workers
            Actor.set_status(actor_id, ERROR, status_message=e.message)
            for worker in workers:
                try:
                    self.kill_worker(worker)
                except DockerError as e:
                    logger.info("Received DockerError trying to kill worker: {}. Exception: {}".format(worker, e))
                    logger.info("Spawner will continue on since this is exception processing.")
            raise SpawnerException(message=e.message)
        return channels, anon_channels, workers

    def start_worker(self, image, tenant, worker_id):
        ch = WorkerChannel()
        # start an actor executor container and wait for a confirmation that image was pulled.
        worker_dict = run_worker(image, ch.name, worker_id)
        worker = Worker(tenant=tenant, **worker_dict)
        logger.info("worker started successfully, waiting on ack that image was pulled...")
        result = ch.get()
        logger.debug("Got response back from worker. Response: {}".format(result))
        if result.get('status') == 'error':
            # there was a problem pulling the image; put the actor in an error state:
            msg = "Got an error back from the worker. Message: {}",format(result)
            logger.info(msg)
            if 'msg' in result:
                raise SpawnerException(message=result['msg'])
            else:
                logger.error("Spawner received invalid message from worker. 'msg' field missing. Message: {}".format(result))
                raise SpawnerException(message="Internal error starting worker process.")
        elif result['value']['status'] == 'ok':
            logger.debug("received ack from worker.")
            return ch, result['reply_to'], worker
        else:
            msg = "Got an error status from worker: {}. Raising an exception.".format(str(result))
            logger.error("Spawner received an invalid message from worker. Message: ".format(result))
            raise SpawnerException(msg)

    def kill_worker(self, worker):
        pass

def main():
    # todo - find something more elegant
    idx = 0
    while idx < 3:
        try:
            sp = Spawner()
            logger.info("spawner made connection to rabbit, entering main loop")
            logger.info("spawner using abaco_conf_host_path={}".format(os.environ.get('abaco_conf_host_path')))
            sp.run()
        except rabbitpy.exceptions.ConnectionException:
            # rabbit seems to take a few seconds to come up
            time.sleep(5)
            idx += 1
    logger.critical("spawner could not connect to rabbitMQ. Shutting down!")

if __name__ == '__main__':
    main()
