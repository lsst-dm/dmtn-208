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

#. The portions of the image cutout service that implement general IVOA standards, such as `DALI`_ and `UWS`_ components, will be designed to be separated into a library or framework and reused in future services.
   The implementation will also serve as a model from which we will derive a template for future IVOA API services.

#. The Butler should be used as the data store for all astronomy data objects.

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

Image cutouts will be stored in a Butler collection alongside their associated metadata.
A request for the FITS file of the cutout will be served from that Butler collection.

Metadata about requests will be stored by the cutout workers in a SQL database, using CloudSQL for installations on Google Cloud Platform and an in-cluster PostgreSQL server elsewhere.
The same SQL store will be used by the API service to satisfy requests for async job lists, status, and other metadata.

The storage used by cutout results will be temporary and garbage-collected after some time.
The expected lifetime is on the order of weeks.
Users who wish to preserve the results for longer will need to transfer their data to a Butler collection in their own working space.

Here is the overall design in diagram form.

.. figure:: /_static/architecture.png
   :name: Image cutout service architecture

   Image cutout service architecture

API service
===========

The service frontend providing the SODA API will use the `FastAPI`_ framework.

Input parameters
----------------

The image cutout service requirements in `LDM-554`_ state that support for ``POLYGON`` requests is optional.
The API will support them if the underlying pipeline task supports them.
(Note that if ``POLYGON`` is not supported, we cannot advertise the ``POS`` capability, since that capability requires support for ``POS=POLYGON``.)

Multiple ``ID`` parameters and multiple filter parameters may be given.
``TIME`` and ``POL`` filter parameters will not be supported.
``BAND`` filter parameters will not be supported in the initial implementation.
They may become meaningful later in cutout requests from all-sky coadds and can be added at that time.

If the requested cutout does not lie within the bounds of any image named in the ``ID`` parameters, the entire cutout request will fail with an appropriate error.

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
Once it has completed, the client can retrieve metadata about the job, including a list of reuslts, and then retrieve each result separately.

To avoid unnecessarily multipling API implementations, the sync mode will be implemented as a wrapper around the async mode using the implementation strategy described in the `UWS`_ specification.
Specifically, a sync request will start an async job, redirect to a URL that blocks on the async job, and then redirect to the primary result URL for the async job.

Further considerations for UWS support and async jobs are discussed in :ref:`uws-impl`.

XML handling
------------

IVOA standards unfortunately require use of XML instead of JSON.
Every available XML processing library for Python has `known security concerns`_ around at least denial of service attacks and, in some cases, more serious vulnerabilities.
User-supplied XML must therefore be handled with caution.

.. _known security concerns: https://docs.python.org/3/library/xml.html#xml-vulnerabilities

The image cutout service will use `defusedxml`_ as a wrapper around all parsing of XML messages to address this concern.

.. _defusedxml: https://pypi.org/project/defusedxml/

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

To ensure the cutout operation is performed by properly-vetted scientific code, the image cutout will be done via a task.
For some types of cutouts, such as cutouts from PVIs that must be reconstructed from raw images, this may be a pipeline task.

Currently, pipeline tasks must be invoked via the command line, but the expectation is that pipelines will add a way of invoking a pipeline task via a Python API.
Once that is available, each cutout worker can be a long-running Python process that works through a queue of cutout requests, without paying the cost of loading Python libraries and preparing supporting resources for each cutout action.

.. _results:

Results
=======

Result format
-------------

All cutout requests will create a FITS file stored in a new output Butler collection.
The metadata about the request that would be returned as metadata for a UWS async job (see :ref:`task-storage`) will also be stored in that Butler collection so that the collection has the provenance of the cutouts.

The primary output of a cutout operation in the initial implementation will be a single FITS file.
Each filtering parameter produces a separate cutout.
The cutout images will be stored as extensions in the result FITS file, not in the Basic FITS HDU.

The result of a sync request that does not request an alternate image format is the FITS file.
Therefore the sync API will redirect to the FITS file result of the underlying async job.

The full result of an async request will list at least two results: the FITS file, and the URL or other suitable Butler identifier for the output Butler collection that contains both that FITS file and the metadata about hte cutout request.

When client/server Butler is available, the primary result will be provided via a redirect to a signed link for the FITS file in the collection.
Until that time, it will be an unsigned redirect to the object store URL, and we will make the object store public (but with a random name).

These URLs or identifiers will be stored in the SQL database that holds metadata about async jobs and retrieved from there by the API service to construct the UWS job status response.

Because the image will be retrieved directly from the underlying object store, the ``Content-Type`` metadata for files downloaded directly by the user must be correct in the object store.
Butler currently does not set ``Content-Type`` metadata when storing objects.
The current plan is to have ButlerURI automatically set the ``Content-Type`` based on the file extension, and ensure that files stored in a output Butler collection have appropriate extensions.

Alternate image types
~~~~~~~~~~~~~~~~~~~~~

If another image type is requested, it will be returned alongside (not replacing) the FITS image.
If another image type is requested and multiple cutouts are requested via multiple filter parameters, each converted cutout will be a separate entry in the result list for the job.
The converted images will be stored in the output Butler collection alongside the FITS image and the request metadata.

If an alternate image type is requested, the order of results for the async job will list the converted images in the requested image type first, followed by the FITS file, and then the Butler collection that contains all of the outputs.
As with the FITS file, the images will be returned via signed links to the underlying object store with client/server Butler, and unsigned links to the object store until client/server Butler is available.

Sync requests that also request an alternate image type must specify only one filter parameter, since only one image can be returned via the sync API and the alternate image types we expect to support, unlike FITS, do not allow multiple images to be included in the same file. [#]_
This will be enforced by the service frontend.

.. [#] The result of a sync request with multiple filters and an alternate image type could instead be a collection (such as a ZIP file) holding multiple images.
   However, this would mean the output MIME type of a sync request would depend on the number of filter parameters, which is ugly, and would introduce a new requirement for generating output collections that are not Butler collections.
   It is unlikely there will be a compelling need for a sync request for multiple cutouts with image conversion.
   That use case can use an async request instead.

Result data retention
---------------------

The output Butler collections will be read-only for the user (to avoid potential conflicts with running tasks from users manipulating the collections) and will be retained for a limited period of time (to avoid unbounded storage requirements for cutouts that are no longer of interest).
If the user who requested a cutout wishes to retain it, they should transfer the result Butler collection into their own or some other shared space.
Alternately (and this is the expected usage pattern for sync requests and one-off exploratory requests), they can retrieve only the FITS file of the cutout and allow the full Butler collection to be automatically discarded later.

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

Therefore, each worker task will take responsibility for storing its own metadata, as well as the cutout results, in external storage.
On either success or failure, the task metadata (success or failure, any error message, the request parameters, and the other metadata for a job required by the UWS specification) will be stored in a SQL database independent of the task queue system.
As described in :ref:`results`, if the request is successful, the same metadata will also be stored in the output Butler collection.

The image cutout web service will then use the SQL database to retrieve information about finished jobs, and ask the task queuing system for information about still-running jobs that have not yet stored their result metadata.
This will satisfy the UWS API requirements.

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
Following the same principle, we will use Redis.
(As discussed in :ref:`task-storage`, the task result will only be used for task metadata.
The result of the cutout operation will be stored in the Butler, and the task metadata will separately be stored in a SQL database to satisfy the requirements for the UWS API.)

Aborting jobs
-------------

Neither Celery nor Dramatiq support cancellation of a task once it begins executing.
(See `Bogdanp/dramatiq#37 <https://github.com/Bogdanp/dramatiq/issues/37>`__ for some discussion and a way to implement task cancellation as a customization to Dramatiq.)

It's not clear whether this feature will be necessary.
It would be useful if a user accidentally started a resource-intensive request and then realized there was an error in the request and the results would be useless.
However, it's not yet clear whether that case will be common enough to warrant the implementation complexity.

Therefore, the initial implementation will not support aborting a UWS job if that job has already started.
Posting ``PHASE=ABORT`` to the job phase URI will therefore return a 303 redirect to the job URI but will not change the phase.
(The UWS spec appears to require this behavior.)

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

#. Should we support an extension to SODA that allows the filter parameters to be provided as a VOTable?
