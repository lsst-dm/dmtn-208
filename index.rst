:tocdepth: 1

.. sectnum::

Abstract
========

The IVOA `SODA`_ (Server-side Operations for Data Access) standard will be used to implement an image cutout service for the Rubin Science Platform following the requirements in `LDM-554`_ 4.2.3 and the architecture specified in `DMTN-139`_ (not yet published).
This document discusses implementation considerations and proposes an implementation strategy that uses `Dramatiq`_ as a work-queuing system, a `pipeline task`_ to perform the cutout, and Butler as the data store.

.. _SODA: https://ivoa.net/documents/SODA/20170517/REC-SODA-1.0.html
.. _LDM-554: https://ldm-554.lsst.io/
.. _DMTN-139: https://dmtn-139.lsst.io/
.. _Dramatiq: https://dramatiq.io/
.. _pipeline task: https://pipelines.lsst.io/

Implementation goals
====================

This design satisfies the following high-level goals:

#. Following SQuaRE's standards for new web APIs, the web API layer should use FastAPI.
   This will satisfy the desired feature from `DMTN-139`_ that each web service publish an OpenAPI v3 service description, since FastAPI does that automatically.

#. There must be a clear division of responsibility between the service framework, which implements the IVOA API, and the data manipulation that produces the cutout.
   This is so that data manipulation is applied consistently in both Rubin data processing and VO services, and so that the service takes advantage of code validated as part of the science pipeline QA process. 

#. The worker processes that perform the data manipulation must be long-running so that the startup costs of loading relevant Python libraries and initializing external resources do not impact the performance of image cutout requests.

#. All locally-written software should be written in Python, as the preferred implementation language of the Rubin Observatory.

#. The service frontend must not require the full Rubin Observatory stack.
   This causes too many complications for building containers and coordinating Python package versions.
   Only the worker processes should require the stack, and should have a minimum of additional dependencies to limit how many Python modules we need to add to or layer on top of the stack.

#. The portions of the image cutout service that implement general IVOA standards, such as `DALI`_ and `UWS`_ components, will be designed to be separated into a library or framework and reused in future services.
   The implementation will also serve as a model from which we will derive a template for future IVOA API services.

#. The Butler should be used as the underlying data store for all Rubin Observatory data objects.

#. Cutouts should be retrieved directly from the underlying data store that holds them, rather than retrieved and then re-sent by an intermediate web server.
   This avoids performance issues with the unnecessary middle hop and avoids having to implement such things as streaming or chunking in an intermediate server.

.. _DALI: https://www.ivoa.net/documents/DALI/20170517/REC-DALI-1.1.html
.. _UWS: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html

Architecture summary
====================

The image cutout service will be a `FastAPI`_ Python service running as a Kubernetes ``Deployment`` inside the Rubin Science Platform and, as with every other RSP service, using `Gafaelfawr`_ for authentication and authorization.
Image cutout requests will be dispatched via `Dramatiq`_ to worker processes created via a separate Kubernetes ``Deployment`` in the same cluster.
A high-availability `Redis`_ cluster will be used as the message bus and task result store for Dramatiq.

.. _FastAPI: https://fastapi.tiangolo.com/
.. _Gafaelfawr: https://gafaelfawr.lsst.io/

Image cutouts will be stored in a temporary Butler collection.
The results of the cutout request will be served from that collection.

The Dramatiq workers will be separated into two pools using different queues.
One will perform the cutouts and will run on the stack container.
The other, smaller pool will perform database housekeeping and store results or errors.

The storage used by cutout results will be temporary and garbage-collected after some time.
The expected lifetime is on the order of weeks.
Users who wish to preserve the results for longer will need to transfer their data elsewhere, such as local storage, their home directory on the Rubin Science Platform, or a personal Butler collection.

Metadata about requests will be stored by the cutout workers in a SQL database, using CloudSQL for installations on Google Cloud Platform and an in-cluster PostgreSQL server elsewhere.
The same SQL store will be used by the API service to satisfy requests for async job lists, status, and other metadata.

Here is the overall design in diagram form.

.. figure:: /_static/architecture.png
   :name: Image cutout service architecture

   Image cutout service architecture

API service
===========

The service frontend providing the SODA API will use the `FastAPI`_ framework.

Input parameters
----------------

SODA calls parameters that control the shape of the cutout "filtering parameters" or "filters."
The word filter is overloaded in astronomy, so this document instead calls those parameters "stencils."

The image cutout service requirements in `LDM-554`_ state that support for ``POLYGON`` requests is optional.
The API will support them if the underlying pipeline task supports them.
(Note that if ``POLYGON`` is not supported, we cannot advertise the ``POS`` capability, since that capability requires support for ``POS=POLYGON``.)

The initial version of the cutout service will only support a single ``ID`` parameter and a single stencil parameter.
It is likely that we will support multiple stencils and multiple ``ID`` parameters in a future version of the service, but we may not use the API described in SODA for more complex operations, since its requirements for outputs and error reporting do not match our needs.

``TIME`` and ``POL`` stencil parameters will not be supported.
``BAND`` stencil parameters will not be supported in the initial implementation.
They may become meaningful later in cutout requests from all-sky coadds and can be added at that time.

The initial implementation of the image cutout service will only return FITS files.
However, we expect to need support for other image types such as JPEG in the future.
When that support is added, it can be requested via a ``RESPONSEFORMAT=image/jpeg`` parameter.

The `UWS`_ specification supports providing a quote for how long an async query is expected to take before it is started.
The initial implementation will always set the quote to ``xsi:nil``, indicating that it does not know how long the request will take.
However, hopefully a future improvement of the service will provide real quote values based on an estimate of the complexity of the cutout request, since this information would be useful for users deciding whether to make a complex cutout request.

API modes
---------

The SODA specification supports two API modes: sync and async.
A sync request performs and operation and returns the result directly.
An async operation creates a pending job, which can then be configured and executed.
While executing, a client can poll the job to see if it has completed.
Once it has completed, the client can retrieve metadata about the job, including a list of results, and then retrieve each result separately.

To avoid unnecessarily multiplying API implementations, the sync mode will be implemented as a wrapper around the async mode.
Specifically, a sync request will start an async job, wait for that job to complete, and then redirect to the primary result URL for the async job.

Further considerations for UWS support and async jobs are discussed in :ref:`uws-impl`.

Permission model
----------------

For the stateful async protocol, all created jobs are associated with a user.
Only that user has access to the jobs they create.
Attempts to access jobs created by other users will return authorization errors.

In the initial implementation, there is no concept of an administrator role or special async API access for administrators.
Administrators can directly inspect the database if needed, or can impersonate a user if necessary.
Administrative access to the API without impersonation may be added in future versions if this proves useful.

Access control is done via Gafaelfawr_.
Image cutout service access is controlled via the ``read:image`` scope.

Quotas and throttling
---------------------

The initial implementation of the image cutout service will not support either quotas or throttling.
However, we expect support for both will be required before the production launch of the Rubin Science Platform.
Implementation in the image cutout service (and in any other part of the API Aspect of the Rubin Science Platform) depends on an implementation of a general quota service for the RSP that has not yet been designed or built.

Quotas will be implemented in the service API frontend.
Usage information will be stored in the same SQL database used to store job metadata and used to make quota decisions.

Throttling will be implemented the same way, using the same data.
Rather than rejecting the request as with a quota limit, throttled requests may be set to a lower priority when dispatched via Dramatiq so that they will be satisfied only after higher-priority requests are complete.
If we develop a mechanism for estimating the cost of a request, throttling may also reject expensive requests while allowing simple requests.

If the service starts throttling, sync requests may not be satisfiable within a reasonable HTTP timeout interval.
Therefore, depending on the severity of the throttling, the image cutout service may begin rejecting sync requests from a given user and requiring all requests be async.

All of these decisions will be made by the API service layer when the user attempts to start a new job or makes a sync request.

.. _cutout:

Performing the cutout
=====================

To ensure the cutout operation is performed by properly-vetted scientific code, the image cutout will be done via a pipeline.
This will allow multi-step cutout operations, such as cutouts from PVIs that must be reconstructed from raw images.

The cutout pipeline is responsible for propagating provenance metadata from the source data and the cutout parameters into the resulting FITS file, or into appropriate metadata in the output files for other image types.
See `PipelineTask-level provenance in DMTN-185 <https://dmtn-185.lsst.io/#pipelinetask-level-provenance>`__ for discussion of provenance metadata in general.

Currently, pipelines must be invoked via the command line, but the expectation is that the project will add a way of invoking a pipeline via a Python API.
Once that is available, each cutout worker can be a long-running Python process that works through a queue of cutout requests, dispatching each to a pre-built pipeline, without paying the cost of loading Python libraries and preparing supporting resources for each cutout action.

Once there is a client/server Butler service, Butler operations to perform the cutout and to store the cutout result will be done as the user requesting the cutout, using a delegated internal token as described in `SQR-049`_.

.. _SQR-049: https://sqr-049.lsst.io/#internal-tokens

.. _task-mapping:

Mapping cutout requests to pipelines
------------------------------------

We expect cutout requests to map to different pipelines depending on the details of the request.
For example, the ``ID`` may specify a virtual data product, which may require a pipelines to construct that virtual data product before performing a cutout.
We may also want to implement each cutout stencil type as a separate pipeline.

Each possible pipeline will have its own registered Dramatiq actor.
The input parameters to that pipeline will be the arguments to that actor.
Because Dramatiq is used to execute the actor, all arguments must be JSON-serializable (so, for example, we will pass Python floats rather than Astropy SkyCoord objects).
The actor will then store the input parameters into an input Butler collection for the pipeline, if necessary.

The logic to map an incoming SODA request to a pipeline (and thus its actor) and input parameters will be done in the API frontend as the request is received so that any errors can be immediately reported to the user.
This mapping may require retrieving metadata about the dataset specified by the ``ID`` parameter from another service (probably the Butler).

.. _worker-queue:

Worker queue design
-------------------

The worker processes run in a container built on top of the Rubin Observatory stack.
Unfortunately, stack containers are so large that they cannot be constructed in GitHub Actions.
This makes building new containers annoyingly difficult, so we want to minimize how frequently it needs to be done.
Ideally, we would avoid creating a new derivative container entirely.

Once a job has been created via the frontend and queued, workers must perform the following actions:

- Parse and store the input parameters in a format suitable for performing the cutout with a pipeline task.
- Update the UWS job status to indicate execution is in progress.
- Perform the cutout, storing the results in an output Butler collection.
- Update the UWS job status to indicate execution is complete and store a pointer to the output Butler collection.
- If the cutout job failed, instead update the UWS job to indicate the job errored, and store the error message in the UWS database.

The simplest design would be to give the worker credentials for the UWS database and have it perform all of those actions directly, via a common UWS wrapper around an arbitrary worker process.
However, the cutout work has to run in the stack, but the wrapper would need access to the database schema, the input parameter parser, and all of the resulting dependencies.
This would require adding a significant amount of code on top of the stack container, which is not desirable for the reasons mentioned above.

A slightly more complex queuing structure can address this problem.
Instead of a single cutout function (an "actor" in the Dramatiq vocabulary), define four actors (names given in parentheses):

#. The cutout actor itself, which takes a (JSON-serializable) list of arguments specifying the ``ID`` and cutout stencil [#]_.  (``cutout``)
#. An actor that marks the UWS job as executing.  (``job_start``)
#. An actor that marks the UWS job as complete and saves a pointer to the Butler output collection.  (``job_complete``)
#. An actor that marks the UWS job as failed and saves the error message in the UWS database.  (``job_failed``)

.. [#] This is a minor simplification.
       There will likely be multiple cutout actors since there will be a one-to-one correspondence between cutout actors and pipeline tasks.
       The frontend will decide which actor to execute given the input parameters, as discussed in :ref:`task-mapping`.

The first actor will use the ``cutout`` queue.
The other three actors will use the ``uws`` queue.
Now, only the first actor needs to be run in a stack container.
The workflow looks like this:

#. Parse the input parameters in the frontend, determine the specific cutout actor to run, and pass them as a JSON-serializable list of arguments to the cutout actor.
   Include the job ID as a parameter.
#. As part of that message, set ``on_success`` and ``on_failure`` `Dramatiq callbacks`_ pointing to ``job_complete`` and ``job_failed``, respectively.
#. As its first action, the cutout actor sends a message to ``job_start`` with the job ID and timestamp.
#. When the cutout actor finishes, either ``job_complete`` or ``job_failed`` will be called automatically.

.. _Dramatiq callbacks: https://dramatiq.io/cookbook.html#callbacks

Then, run two pools of workers.
One is configured to only watch the ``cutout`` queue and is the one that does the actual work.
These workers will run on a stack container.
The other, smaller pool of workers will only watch the ``uws`` queue and do database housekeeping.

With this separation, the frontend and ``uws`` queue workers can share code, including the database schema, but only need a stub for the ``cutout`` actor.
Similarly, the ``cutout`` actor only needs to contain the code for performing the cutout, and can contain only stubs for the ``job_start``, ``job_complete``, and ``job_failed`` actors.

The Dramatiq result store will be used to pass a pointer to Butler output collection from the ``cutout`` actor to the ``job_complete`` actor, and any exceptions from the ``cutout`` actor to the ``job_failed`` actor.

Worker containers
-----------------

Given this worker queue design, the worker container can be a generic stack container plus the following:

#. The results of ``pip install dramatiq[redis]``, so that the worker can talk to the message queue and result store.
#. The code for performing the cutout.
   This is expected to be a single (short) file that performs any necessary setup for the pipeline task.

For the initial implementation, we will create a new virtualenv in a Kubernetes ``emptyDir`` temporary file system on container startup, using the stack Python, and ``pip install dramatiq[redis]`` in that virtualenv.
The code for the cutout actor (and the stubs for the other actors it may call) will be stored in a Kubernetes ``ConfigMap`` and mounted into the container.
The worker will then be started by running ``dramatiq`` within that virtualenv, on the mounted cutout code, with the stack activated.

This completely avoids building new containers based on the stack container, at the cost of somewhat slow startup and extra traffic to PyPI on each container restart.

If this implementation proves successful, we will look at ways to optimize it, such as including ``dramatiq[redis]`` in the stack or building a derived container with Dramatiq already installed.

Interface contract
------------------

This is the interface contract with the pipelines that will perform cutouts.
This is sufficient for the initial implementation, which only supports a single cutout stencil on a single ``ID`` parameter.
We expect to add multiple ``ID`` parameters and possibly multiple cutout stencils in future revisions of the service.

Also see `DM-32097`_.

.. _DM-32097: https://jira.lsstcorp.org/browse/DM-32097

Input
~~~~~

- An ``ID``, as a string, that uniquely identifies the source image on which to perform the cutout.
  These are opaque to the image cutout service but must match the data IDs returned by ObsTAP queries, SIA, etc.
  The requirements for the image cutout service specify that the data ID may refer to a raw, PVI, compressed-PVI, diffim, or coadded image.
  Alternately (and probably preferably), a function that transforms such an ``ID`` string into an appropriate Butler data query.
  (This may be different for different pipelines.)

- A cutout stencils. There are three possible stencil types:

  - Circle, specified as an Astropy SkyCoord in ICRS for the center and an Astropy Angle for the radius.

  - Polygon, specified as an Astropy SkyCoord containing a sequence of at least three vertices in ICRS.
    The line from the last vertex to the first vertex is implicit.
    Vertices must be ordered such that the polygon winding direction is counter-clockwise (when viewed from the origin toward the sky), but the frontend doesn't know how to check this so the backend may need to.

  - Range, specified as a pair of minimum and maximum ra values and a pair of minimum and maximum dec values, in ICRS, as doubles.
    The minimums may be ``-Inf`` and/or the maximums may be ``+Inf`` to indicate an unbounded range extending to the boundaries of the image.

- The Butler collection in which to store the output.

We assume the input should be provided as a Butler data query and as a table in an input Butler collection.
The pipeline should specify the format of that input Butler collection and the Butler type or types to use.
As with the ``ID`` transformation, this may be different for different pipelines.

Polygon is optional in our formal requirements, but range stencils cannot be advertised with an IVOA capability unless we implement polygons, so it would be good if we could do so.

The long-term goal is to have some number of image cutout backends that are busily performing cutouts as fast as they can, since we expect this to be a popular service with a high traffic volume.
Therefore, as much as possible, we want to do setup work in advance so that each cutout will be faster.
For example, we want cutouts to be done in a long-running process that pays the cost of importing a bunch of Python libraries just once during startup, not for each cutout.

Output
~~~~~~

The output cutout should be a FITS image stored in the output Butler collection.

The FITS file should contain metadata recording the input parameters, time at which the cutout was performed, and any other desirable provenance information.
(This can be postponed to a later revision of the pipeline.)


Errors
~~~~~~

A cutout area that's not fully contained within the specified image is an error (except for unbounded ranges).
The current SODA standard requires that this error be handled by returning success to the async job but setting the result to a ``text/plain`` document starting with an error code.
This seems highly unexpected and undesirable, so we will not be following that approach.
Instead, the operation should abort with an error if the cutout area is not fully contained in the specified image.

Errors can be delivered in whatever form is easiest as long as the frontend can recover the details of the error.
(For example, an exception is fine as long as the user-helpful details of the error are in the exception.)

.. _cutout-future:

Future work
~~~~~~~~~~~

We expect to add support for specifying the output image format and thus request a JPEG image (or whatever else makes sense).
This presumably would map to a different pipeline.

In the future, we will probably support multiple ``ID`` parameters and possibly multiple stencils.
When supported, the semantics of multiple ``ID`` values and multiple stencils are combinatorial: in other words, the requested output is one cutout for each combination of ``ID`` and stencil.
So two ``ID`` values and a set of stencils consisting of two circles and one polygon would produce six cutouts: two circles and one polygon on both of the two ``ID`` values.

For cutouts with multiple ``ID`` parameters or multiple stencils, there is some controversy currently over whether to return a single FITS file with HDUs for each cutout, or to return N separate FITS files.
The current SODA standard requires the latter, but we had thought the former would be easier to work with.
Because of this and the error handling problem discussed above, we may deviate from the SODA image cutout standard and define our own SODA operations that returns a single FITS file with improved error handling.

.. _results:

Results
=======

Result format
-------------

All cutout requests will create a FITS file.
A cutout request may also create additional output files if alternate image types are requested.

The job representation for a successful async request in the initial implementation will be a single FITS file.
The cutout image will be stored as an extension in the FITS file, not in the Basic FITS HDU.
This output should use a ``Content-Type`` of ``application/fits`` [#]_.

.. [#] ``image/fits`` is not appropriate since no image is returned in the primary HDU.

Therefore the sync API will redirect to the FITS file result of the underlying async job.

As discussed in :ref:`cutout-future`, there is some controversy over the output format when multiple ``ID`` parameters or stencils are provided.
The initial implementation will not support this.

When client/server Butler is available, the FITS file will be provided via a redirect to a signed link for the location of the FITS file in the object store underlying the Butler collection.
Signed URLs are temporary and may have a lifetime shorter than the output Butler collection, so the image cutout service will ask the client/server Butler for new signed URL each time the job results are requested (possibly with caching of up to an hour).
The URL of the job result may therefore change, although the underlying objects will stay the same, and the client should not save the URL for much later use.

Until client/server Butler is available, the URL of the FITS file will be an unsigned redirect to the object store URL, and we will make the object store public (but with a random name).
The same will be done for alternate image output formats.

The SQL database that holds metadata about async jobs will hold the information required to request or reconstruct the URL of the FITS file.
That information will be retrieved from there by the API service and used to construct the UWS job status response.

Because the image will be retrieved directly from the underlying object store, the ``Content-Type`` metadata for files downloaded directly by the user must be correct in the object store.
Butler currently does not set ``Content-Type`` metadata when storing objects.
The current plan is to have ButlerURI automatically set the ``Content-Type`` based on the file extension, and ensure that files stored in a output Butler collection have appropriate extensions.

Alternate image types
~~~~~~~~~~~~~~~~~~~~~

This section describes future work that will not be part of the initial implementation.

If another image type is requested, it will be returned alongside (not replacing) the FITS image.
If another image type is requested and multiple cutouts are requested via multiple stencil parameters, each converted cutout will be a separate entry in the result list for the job.
The converted images will be stored in the output Butler collection alongside the FITS image.

If an alternate image type is requested, the order of results for the async job will list the converted images in the requested image type first, followed by the FITS file.
As with the FITS file, the images will be returned via signed links to the underlying object store with client/server Butler, and unsigned links to the object store until client/server Butler is available.

The response to a sync request specifying an alternate image type will be a redirect to an object store link for the converted image of that type.
Sync requests that request an alternate image type must specify only one stencil parameter, since only one image can be returned via the sync API and the alternate image types we expect to support, unlike FITS, do not allow multiple images to be included in the same file. [#]_
This will be enforced by the service frontend.

.. [#] The result of a sync request with multiple stencils and an alternate image type could instead be a collection (such as a ZIP file) holding multiple images.
       However, this would mean the output MIME type of a sync request would depend on the number of stencil parameters, which is ugly, and would introduce a new requirement for generating output collections that are not Butler collections.
       It is unlikely there will be a compelling need for a sync request for multiple cutouts with image conversion.
       That use case can use an async request instead.

Result storage
--------------

The output Butler collection will be retained only for a limited period of time (to avoid unbounded storage requirements for cutouts that are no longer of interest).
It will be read-only for the user (to avoid potential conflicts with running tasks from users manipulating the collections) once ACLs for Butler collections are available through client/server Butler.

If the user who requested a cutout wishes to retain it, they should store the outputs in local storage, their home directory in the Rubin Science Platform, a personal Butler collection, or some other suitable location.

The `SODA`_ specification also allows a request to specify a VOSpace location in which to store the results, but does not specify a protocol for making that request.
The initial implementation of the image cutout service will not support this, but it may be considered in a future version.

.. _uws-impl:

UWS implementation
==================

The IVOA `UWS`_ (Universal Worker Service) standard describes the behavior of async IVOA interfaces.
The image cutout service must have an async API to support operations that may take more than a few minutes to complete, and thus requires a UWS implementation to provide the relevant API.
We will use that implementation to perform all cutout operations.

After a survey of available UWS implementations, we chose to write a new one on top of the Python `Dramatiq`_ distributed task queue.

.. _task-storage:

Task result storage
-------------------

An image cutout task produces two types of output: the cutouts themselves with their associated astronomical metadata, and the metadata about the request.
The latter includes the parameters of the cutout request, the job status, and any error messages.

The task queuing system is the natural store for the task metadata.
However, even with a configured result store, the task queuing system only stores task metadata while the task is running and for a short time afterwards.
The intent of the task system is for the invoker of the task to ask for the results, at which point they are delivered and then discarded.

The internal result storage is also intended for small amounts of serializable data, not for full image cutouts.
The natural data store for image cutouts is a Butler collection.

Therefore, each worker task will take responsibility for storing the cutout results in external storage using the Butler.
We will use the system described in :ref:`worker-queue` to route the pointer to that external storage to an actor that will update the UWS database with appropriate results.

The task metadata (success or failure, any error message, the request parameters, and the other metadata for a job required by the UWS specification) will be stored in a SQL database external to the task queue system.
The parameters known before job execution (such as the request parameters) will be stored by the frontend.
The other data will be stored by specialized Dramatiq actors via callbacks triggered by the success or failure of the cutout actor.
The image cutout web service will then use the SQL database to retrieve information about finished jobs, and ask the task queuing system for information about still-running jobs that have not yet stored their result metadata.
This will satisfy the UWS API requirements.

We will use Dramatiq result storage, but only to pass the name of the output Butler collection from the cutout actor to the actor that will store that in the database.

Summary of task queuing system survey
-------------------------------------

Since both the API frontend and the image cutout pipeline task will be written in Python, a Python UWS implementation is desirable.
An implementation in a different language would require managing it as an additional stand-alone service that the API frontend would send jobs to, and then finding a way for it to execute Python code with those job parameters without access to Python libraries such as a Butler client.
We therefore ruled out UWS implementations in languages other than Python.

`dax_imgserv`_, the previous draft Rubin Observatory implementation of an image cutout service, which predates other design discussions discussed here, contains the skeleton of a Python UWS implementation built on `Celery`_ and `Redis`_.
However, job tracking was not yet implemented.

.. _dax_imgserv: https://github.com/lsst/dax_imgserv/
.. _Celery: https://docs.celeryproject.org/en/stable/index.html
.. _Redis: https://redis.io/

`uws-api-server`_ is a more complete UWS implementation that uses Kubernetes as the task execution system and as the state tracking repository for jobs.
This is a clever approach that minimizes the need for additional dependencies, but it requires creating a Kubernetes ``Job`` resource per processing task.
The resulting overhead of container creation is expected to be prohibitive for the performance and throughput constraints required for the image cutout service.
This implementation also requires a shared POSIX file system for storage of results, but we want to align the image cutout service with the project direction towards a `client/server Butler`_ and use Butler as the object store for results.
Finally, tracking of completed jobs in this approach is vulnerable to the vagaries of Kubernetes retention of metadata for completed jobs, which may not be sufficiently flexible for our needs.

.. _uws-api-server: https://github.com/lsst-dm/uws-api-server
.. _client/server Butler: https://dmtn-176.lsst.io/

We did not find any other re-usable Python UWS server implementations (as opposed to clients, of which there are several).

Task queue options
------------------

`Celery`_ is the standard Python task queuing system, so it was our default choice unless a different task queue system looked compelling.
However, `Dramatiq`_ appeared to have some advantages over Celery, and there are multiple reports of other teams who have switched to Dramatiq from Celery due to instability issues and other frustration.

Both frameworks are similar, so switching between them if necessary should not be difficult.
Compared to Celery, Dramatiq offers per-task prioritization without creating separate priority workers.
We expect to do a lot of task prioritization to support sync requests, deprioritize expensive requests, throttle requests when the cluster is overloaded, and for other reasons, so this is appealing.
Dramatiq is also smaller and simpler, which is always a minor advantage.

One possible concern with Dramatiq is that it's a younger project primarily written by a single developer.
Celery is the standard task queue system for Python, so it is likely to continue to be supported well into the future.
There is some increased risk with Dramatiq that it will be abandoned and we will need to replace it later.
However, it appears to have growing popularity and some major corporate users, which is reassuring.
It should also not be too difficult to switch to Celery later if we need to.

Dramatiq supports either `Redis`_, `RabbitMQ`_, or Amazon SQS as the underlying message bus.
Both Dramatiq and Celery prefer RabbitMQ and the Celery documentation warns that Redis can lose data in some unclean shutdown scenarios.
However, we are already using Redis as a component of the Rubin Science Platform as a backing store for the authentication system, so we will use Redis as the message bus to avoid adding a new infrastructure component until this is shown to be a reliability issue.

.. _RabbitMQ: https://www.rabbitmq.com/

Dramatiq supports either Redis or Memcache as a store for task results.
However, since we need an external database to store task metadata anyway, we don't need to store task results within the task queue system.
(See :ref:`task-storage` for more details.)

Neither Celery nor Dramatiq support asyncio natively.
Dramatiq is unlikely to add support since the maintainer `is not a fan of asyncio <https://github.com/Bogdanp/dramatiq/issues/238>`__.
For the time being, we'll enqueue tasks synchronously.
Redis should be extremely fast under normal circumstances, so this hopefully won't cause problems.
If it does, we can consider other options, such as the ``asgiref.sync_to_async`` decorator.

Aborting jobs
-------------

In the initial implementation, we won't support aborting jobs.
Posting ``PHASE=ABORT`` to the job phase URL will therefore return a 303 redirect to the job URL but will not change the phase.
(The UWS spec appears to require this behavior.)

In a later version of the service, we will use `dramatiq-abort <https://flared.github.io/dramatiq-abort/>`__ to implement this feature.

Discovery
=========

The not-yet-written IVOA Registry service for the API Aspect of the Rubin Science Platform is out of scope for this document, except to note that the image cutout service will be registered there as a SODA service once the Registry service exists.

The identifiers returned in the ``obs_publisher_did`` column from ObsTAP queries in the Rubin Science Platform must be usable as ``ID`` parameter values for the image cutout service.

In the short term, the result of ObsTAP queries will contain `DataLink`_ service descriptors for the image cutout service as a SODA service.
Similar service descriptors will be added to the results of SIA queries once the SIA service has been written.
This follows the pattern described in section 4.1 of the `SODA`_ specification.

In the longer term, we may instead run a DataLink service and reference it in the ``access_url`` column of ObsTAP queries or via a DataLink "service descriptor" following section 4.2 of the `SODA`_ specification.

.. _DataLink: https://www.ivoa.net/documents/DataLink/20150617/REC-DataLink-1.0-20150617.html

Open questions
==============

#. We need to agree on an identifier format for Rubin Observatory data products.
   This will be used for the ``ID`` parameter.

#. Should we support an extension to SODA that allows the stencil parameters to be provided as a VOTable?

#. SODA requires each cutout parameter return a separate result in the async API, and also requires that each cutout parameter that is invalid given the ``ID`` return, as a result, a ``text/plain`` document that starts with an error label.
   This doesn't seem like what we want.
   We would rather return a single FITS file with all cutouts included, and if any of the cutout parameters are invalid given the data ID, fail the entire job with an error, rather than making the client intuit an error from the MIME type of the result.
   Should we break the standard here?
