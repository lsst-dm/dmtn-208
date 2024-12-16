################################################
RSP image cutout service implementation strategy
################################################

.. abstract::

   The Rubin Science Platform must include an image cutout service following the requirements in :ldm:`554` 4.2.3 and the architecture specified in :dmtn:`139` (not yet published).
   The implementation uses the IVOA SODA_ (Server-side Operations for Data Access) standard.
   This document discusses implementation considerations and describes the chosen implementation strategy, which uses arq_ as a work-queuing system and a separate dedicated GCS bucket as a result store.

.. _SODA: https://ivoa.net/documents/SODA/20170517/REC-SODA-1.0.html
.. _arq: https://arq-docs.helpmanual.io/

Also see :sqr:`096` for discussion of UWS job storage, and :sqr:`063` for discussion of difficulties with implementing the relevant IVOA standards.

Implementation goals
====================

This design satisfies the following high-level goals:

#. Following SQuaRE's standards for new web APIs, the web API layer uses FastAPI_.
   This will satisfy the desired feature from :dmtn:`139` that each web service publish an OpenAPI v3 service description, since FastAPI does that automatically.

#. There must be a clear division of responsibility between the service framework, which implements the API, and the data manipulation that produces the cutout.
   This ensures that data manipulation is applied consistently in both Rubin data processing and VO services, and that the service takes advantage of code validated as part of the science pipeline QA process.

#. The worker processes that perform the data manipulation must be long-running so that the startup costs of loading relevant Python libraries and initializing external resources do not impact the performance of image cutout requests.

#. All locally-written software should be written in Python, as the preferred implementation language of the Rubin Observatory.

#. The service frontend must not require the full Rubin Science Pipelines stack.
   The complexity of the stack causes too many complications for building containers and coordinating Python package versions.
   Only the worker processes should require the stack, and should have a minimum of additional dependencies to reduce the chances of conflicts caused by layering additional Python modules on top of the stack.

#. The portions of the image cutout service that implement general IVOA standards, such as DALI_ and UWS_ components, are separated into a library so that they can be reused for future services.
   In this case, that library is Safir_.
   The implementation also served as a model from which we derived a template for future IVOA API services.

#. Cutouts should be retrieved directly from the underlying data store that holds them, rather than retrieved and then re-sent by an intermediate web server.
   This avoids performance issues with the unnecessary middle hop and avoids having to implement such things as streaming or chunking in an intermediate server.

.. _DALI: https://www.ivoa.net/documents/DALI/20170517/REC-DALI-1.1.html
.. _UWS: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html
.. _Safir: https://safir.lsst.io

Architecture summary
====================

The image cutout service is a FastAPI_ Python service running as a Kubernetes ``Deployment`` inside the Rubin Science Platform and, as with every other RSP service, using Gafaelfawr_ for authentication and authorization.
Image cutout requests are dispatched via arq_ to worker processes created via a separate Kubernetes ``Deployment`` in the same cluster.
Redis_ is used as the message bus and temporary metadata result store for arq.

.. _FastAPI: https://fastapi.tiangolo.com/
.. _Gafaelfawr: https://gafaelfawr.lsst.io/
.. _Redis: https://redis.io/

Image cutouts are stored in a dedicated :abbr:`GCS (Google Cloud Storage)` bucket with an object expiration time set (initially, 30 days), so cutouts are automatically purged after some period of time.
The results of the cutout request are served from that bucket using signed URLs.

There are two pools of arq workers using separate queues.
One performs the cutouts and uses workers built on the Rubin Science Pipelines container.
The other, smaller pool stores results or errors in the UWS job storage database.
This is done via a separate backend API service named Wobbly_.
See :sqr:`096` for details of the UWS job storage design.

.. _Wobbly: https://github.com/lsst-sqre/wobbly

Wobbly is also used to satisfy requests for async job lists, status, and other metadata.

Users who wish to preserve the results for longer than the object expiration time of the results bucket will need to transfer their data elsewhere, such as local storage, their home directory on the Rubin Science Platform, or a personal Butler collection.

Here is the overall design in diagram form.

.. diagrams:: architecture.py

This is slightly simplified.
Both Wobbly and Butler are also Gafaelafwr-protected services, so connections to them go through the NGINX ingress and Gafaelfawr rather than going directly.

API service
===========

The service frontend providing the SODA API uses the FastAPI_ framework.

The initial implementation doesn't implement DALI-examples.
This may be added in a later version.

Input parameters
----------------

SODA calls parameters that control the shape of the cutout "filtering parameters" or "filters."
The word filter is overloaded in astronomy, so this document instead calls those parameters "stencils."

The initial implementation supports ``CIRCLE`` and ``POLYGON``.
``POS=RANGE`` and therefore ``POS`` support is more complex and is not supported currently.

``TIME`` and ``POL`` stencil parameters will not be supported.
``BAND`` stencil parameters are not supported in the initial implementation.
They may become meaningful later in cutout requests from all-sky coadds and can be added at that time.

The initial version of the cutout service supports a single ``ID`` parameter and a single stencil parameter.
It is likely that we will support multiple stencils and multiple ``ID`` parameters in a future version of the service or in a separate bulk cutout service.
We may not use the API described in SODA for more complex operations, since its requirements for outputs and error reporting may not match our needs.

The ``ID`` parameter must be the URI to a Butler object uniquely identifying a source image.
Currently, these URIs are of the form ``butler://<tag>/<uuid>``, where ``<tag>`` identifies the Butler repository in which the source image resides. [#]_
The initial implementation only supports cutouts from images that exist in a source Butler collection and thus have a UUID.

.. [#] Changes to this URI format are currently under discussion and are expected to be implemented soon.

Virtual data products will not have a UUID because they will not already exist in a Butler collection, and therefore this ``ID`` scheme cannot be used to identify them.
The most natural way to identify a virtual data product is probably via the Butler data ID tuple.
When cutouts for virtual data products are later implemented, we expect those data products to be identified via a parameter (or set of parameters) other than ``ID``, via an extension to the SODA protocol.
Those parameters would convey the Butler data ID tuple, and the ``ID`` parameter would not be used for such cutouts.

The initial implementation of the image cutout service only returns FITS files.
We expect to need support for other image types such as JPEG in the future.
When that support is added, it can be requested via a ``RESPONSEFORMAT=image/jpeg`` parameter.

The UWS_ specification supports providing a quote for how long an async query is expected to take before it is started.
The initial implementation always sets the quote to ``xsi:nil``, indicating that it does not know how long the request will take.
However, hopefully a future improvement of the service will provide real quote values based on an estimate of the complexity of the cutout request, since this information would be useful for users deciding whether to perform a particular cutout.

The initial implementation does not support changing the job parameters after the creation of an async job but before the job is started.
This may be added in a future version if it seems desirable.

API modes
---------

The SODA specification supports two API modes: sync and async.
A sync request performs and operation and returns the result directly.
An async operation creates a pending job, which can then be configured and executed.
While executing, a client can poll the job to see if it has completed.
Once it has completed, the client can retrieve metadata about the job, including a list of results, and then retrieve each result separately.

To avoid unnecessarily multiplying API implementations, the sync mode is implemented as a wrapper around the async mode.
Specifically, a sync request will start an async job, wait for that job to complete, and then redirect to the primary result URL for the async job.

Further considerations for UWS support and async jobs are discussed in :ref:`uws-impl`.

Permission model
----------------

For the stateful async protocol, all created jobs are associated with a user.
Only that user has access to the jobs they create.
Attempts to access jobs created by other users will return authorization errors.

The underlying image URLs pointing directly to the output cutouts will work for any Internet-connected client, but will expire in 15 minutes.
Those URLs are not guessable, and the cutout service will only provide them to the user who created the cutout request.
If that user wishes to share the results with others, they must download them and put them in some other data store that supports sharing.

There is no concept of an administrator role or special async API access for administrators in the cutout service itself.
Administrators can inspect the UWS job records for any service by talking to the Wobbly service directly.
See :sqr:`096` for more details.

Access control is done via Gafaelfawr_.
Image cutout service access is controlled via the ``read:image`` scope (see :dmtn:`235`).

Quotas and throttling
---------------------

The initial implementation of the image cutout service will not support either quotas or throttling.
However, we expect support for both will be required before the production launch of the Rubin Science Platform.
Eventual implementation will build on the general quota framework for the RSP documented in :sqr:`073`.

Quotas will be implemented in the service API frontend.
Any additional usage information required on top of the normal UWS job metadata will be stored and retrieved via Wobbly.

Throttling will be implemented the same way, using the same data.
The exact throttling implementation has not yet been designed.
The queuing model for arq is strict first-in, first-out without prioritization, so throttling via prioritization would have to be handled in the frontend.
If we develop a mechanism for estimating the cost of a request, throttling may also reject expensive requests while allowing simple requests.

If the service starts throttling, sync requests may not be satisfiable within a reasonable HTTP timeout interval.
Therefore, depending on the severity of the throttling, the image cutout service may begin rejecting sync requests from a given user and requiring all requests be async.

All of these decisions will be made by the API service layer when the user attempts to start a new job or makes a sync request.

.. _cutout:

Performing the cutout
=====================

To ensure the cutout operation is performed by properly-vetted scientific code, the image cutout is done via a separate package that uses the Rubin Science Pipelines stack.
Eventually, this package may also need to perform multi-step cutout operations, such as cutouts from PVIs that must be reconstructed from raw images.
This is not yet implemented.

The cutout backend is responsible for propagating provenance metadata from the source data and the cutout parameters into the resulting FITS file, or into appropriate metadata in the output files for other image types.
See `PipelineTask-level provenance in DMTN-185 <https://dmtn-185.lsst.io/#pipelinetask-level-provenance>`__ for discussion of provenance metadata in general.

The cutout workers are long-running Python processes that work through queues of cutout requests, dispatching each to the code in the cutout backend.
The necessary data for performing the cutout is retrieved via the Butler API using a delegated token so that the API call is done using the user's authentication credentials.
The Butler client and its local cache is shared across requests.

.. _worker-queue:

Worker queue design
-------------------

Once a job has been created via the frontend, workers must perform the following actions:

- Parse and store the input parameters in a format suitable for performing the cutout via the backend.
- Update the UWS job status to indicate execution is in progress.
- Perform the cutout, storing the results in the output GCS bucket.
- Update the UWS job status to indicate execution is complete and store a pointer to the file in the output GCS bucket.
- If the cutout job failed, instead update the UWS job to indicate the job errored, and store the error message in the UWS database.

The current design takes great care to separate the scientific code performing the cutout from all bookkeeping required by the cutout service.
It therefore uses the following workflow:

#. Parse the input parameters in the frontend, determine the specific cutout actor to run, and pass them as a list of arguments to the cutout actor.
   Include the job ID and a delegated token for the user executing the job in those parameters.
#. As the first step of cutout execution, that worker sends another message to a separate ``uws`` arq queue saying that the job has been started.
   This message also includes the delegated token.
#. The arq database worker listens to that queue, processes that message, and updates the UWS job record in Wobbly, using the delegated token for authentication.
#. The main cutout worker continues its work, ending in either successful cutout generation or an error.
   On success, it stores the cutout in GCS and obtains the URL of the new cutout object.
#. Right before finishing, the cutout worker sends a message to the separate ``uws`` arq queue saying that the job is completed.
   This message also includes the delegated token.
#. The arq database worker receives that message, retrieves the results of the cutout worker (which may require polling since it can receive the message before arq realizes the worker is finished), and then stores either the results or the error in Wobbly, using the delegated token for authentication.

The frontend, database worker, and cutout worker all share code, but only the first two install the full dependencies of the service.
The cutout worker only calls a carefully selected subset of the shared code, with minimal dependencies that can be safely installed on top of the Science Pipelines stack container.
The PyPI packages safir-arq_ and safir-logging_ have been separated from the main Safir library and are intended to be suitable for installation on top of a Science Pipelines container for cutout workers (or any other service using this pattern).

.. _safir-arq: https://pypi.org/project/safir-arq/
.. _safir-logging: https://pypi.org/project/safir-logging/

The arq result store is used to pass the cutout results or error from the cutout worker to the database worker.

Errors are repesented by a set of exceptions speciallly designed for that purpose.
These exceptions support the serialization method used by arq and can optionally include serialized backtraces so that error context information can be stored with the job and conveyed back to the user.

Note that this queuing design means that the database updates may be done out of order.
For example, the job may be marked completed and its completion time and results stored, and then slightly later its start time may be recorded.
This may under some circumstances be visible to a user querying the job metadata.
We don't expect this to cause significant issues.

See the `Safir UWS library documentation <https://safir.lsst.io/user-guide/uws/index.html>`__ and :sqr:`096` for more details about this approach.
See :ref:`future-queue-design` for some further discussion of this design and a possible future simplification.

Waiting for job completion
""""""""""""""""""""""""""

Ideally, we should be able to use the task queuing system to know when a job completes and thus to implement the sync API and the UWS requirement for long-polling.
Unfortunately, the queuing strategy used to separate the cutout worker from the database work makes this very difficult to do.
A job is not complete from the user's perspective until the results are stored, but the result storage is done by a separate queued task after the cutout task has completed, and the web frontend has no visibility into the status of that task.
Waiting for the cutout task completion is therefore not sufficient to know that the entire job has completed from the user's perspective.

UWS also requires the server responding to a long-poll request to distinguish between the ``QUEUED`` and ``EXECUTING`` job states, but the move from ``QUEUED`` to ``EXECUTING`` does not trigger message bus activity for the cutout task (it's handled by a separate subtask).

For the initial implementation, we will therefore support the sync API and long polling by polling the database for job status with exponential back-off.
It should be possible to do better than this using the message bus underlying the task queuing system, but a message bus approach will be more complex, so we will hold off on implementation until we know whether the complexity is warranted.

Aborting jobs
"""""""""""""

arq_ is an async task queue framework and expects its worker functions to also be async.
It supports aborting a task by cancelling the async task, which interrupts it at the next async synchronization point.

Unfortunately, all Rubin Science Pipelines code is sync, and therefore does not provide async synchronization points or any way to cancel work in progress.
This makes aborting jobs quite difficult.

Currently, the implementation uses a rather ugly hack: the synchronous cutout worker is run in a single-process `~concurrent.futures.ProcessPoolExecutor`.
If an abort or timeout is received, the async wrapper around that code finds the PID of the worker in the internal data structures of the process pool executor, kills that PID, and then recreates the process pool.

This works, but it has the significant drawback that only one cutout worker can be run at a time on each pod, since aborts and timeouts are implemented by killing the worker and `~concurrent.futures.ProcessPoolExecutor` provides no mechanism to find the worker corresponding to a task.
This is fairly wasteful; a lot of the time in the worker is spent waiting for network activity, and multiple workers in the same pod could share Python code and other memory resources while sharing a single CPU allocation.
It also makes it more difficult to determine when to horizontally scale the worker queue through the normal Kubernetes mechanisms of CPU or memory pressure, since each job is serialized and the worker pod will be mostly idle waiting for network activity.

It is not clear how to address this problem.
Ideally, the cutout backend would be rewritten to be async, at which point this wrapper would no longer be required, multiple async tasks could share a single pod and memory resources, and arq's normal task dispatch, abort, and timeout code would work as intended.
Failing that, we could use a larger process pool executor to handle multiple cutout tasks simultaneously on a single pod, or even switch to `~concurrent.futures.ThreadPoolExecutor` (although the image cutout backend may not be thread-safe), at the cost of losing the ability to implement aborts or timeouts of tasks.

.. _future-queue-design:

Future queue design
"""""""""""""""""""

The original cutout service design predates Wobbly.
The queue workers instead talked directly to the underlying PostgreSQL database to update the job phase and store results or errors.

The simplest design would have been to give the worker credentials for the UWS database and have it perform all of those actions directly, via a common UWS wrapper around an arbitrary worker process.
However, the cutout work has to run on top of the Science Pipelines stack, but the wrapper would need access to the database schema and connection libraries, plus all of the resulting dependencies.
This would have required adding a significant amount of code on top of the stack container, which could have craeted version conflicts between the Python libraries that are part of the stack and the Python libraries used by the other components of the cutout service.
This is the reason for the more complex queuing design that uses two pools of workers and a more complex set of queue messages.

Now that all UWS storage has moved to Wobbly (see :sqr:`096`), the client for updating the UWS job status is much simpler and no longer requires direct access to the database.
We therefore expect to change to a simpler design in the future where the backend worker stores status changes, results, and errors in Wobbly directly (via a wrapper provided by Safir_), thus eliminating the need for the separate database worker pool.

This may make it possible for the frontend to wait for task completion directly, thus solving some of the problems with implementation of UWS long polling.

Worker containers
-----------------

Given this worker queue design, the worker container can be a generic Science Pipelines stack container [#]_ plus the following:

.. [#] Currently, the backend code for performing the cutout is not part of a generic stack container.
       However, the intent is to add it to ``lsst-distrib``.
       See `RFC-828 <https://jira.lsstcorp.org/browse/RFC-828>`__.

#. The results of ``pip install google-cloud-storage httpx safir-arq safir-logging structlog``, so that the worker can talk to the message queue, result store, and Wobbly, and use the standardized logging framework used by the frontend and other Science Platform components.
#. The code for performing the cutout.
   This is installed by installing the full code for the cutout service, but with dependencies disabled.
   The worker function is then very careful about what portions of the cutout service code it references.

This container is built alongside the container shared by the database workers and the frontend.

Interface contract
------------------

This is the interface contract with the backend that will perform cutouts.
This is sufficient for the initial implementation, which only supports a single cutout stencil on a single ``ID`` parameter.
We expect to add multiple ``ID`` parameters and possibly multiple cutout stencils in future revisions of the service.

Also see DM-32097_, which has additional discussion about the initial implementation.

.. _DM-32097: https://jira.lsstcorp.org/browse/DM-32097

Input
"""""

- A Butler client configured to access the appropriate data set represented by the URI in the ``ID`` parameter, and configured to use the user's delegated token for authentication.

- The UUID of the object represented by the ``ID`` parameter.
  This must match the UUID portion of the ID returned by ObsTAP queries, SIA, etc.
  The requirements for the image cutout service specify that ``ID`` may refer to a raw, PVI, compressed-PVI, diffim, or coadded image, but for this initial implementation virtual data products are not supported.

- A single cutout stencil.
  There are three possible stencil types:

  - Circle, specified as an Astropy SkyCoord in ICRS for the center and an Astropy Angle for the radius.

  - Polygon, specified as an Astropy SkyCoord containing a sequence of at least three vertices in ICRS.
    The line from the last vertex to the first vertex is implicit.
    Vertices must be ordered such that the polygon winding direction is counter-clockwise (when viewed from the origin toward the sky), but the frontend doesn't know how to check this so the backend may need to.

  - Range, specified as a pair of minimum and maximum ra values and a pair of minimum and maximum dec values, in ICRS, as doubles.
    The minimums may be ``-Inf`` and/or the maximums may be ``+Inf`` to indicate an unbounded range extending to the boundaries of the image.
    Range will not be supported in the initial implementation.

- The GCS bucket into which to store the resulting cutout.

The long-term goal is to have some number of image cutout backends that are busily performing cutouts as fast as they can, since we expect this to be a popular service with a high traffic volume.
Therefore, as much as possible, we want to do setup work in advance so that each cutout will be faster.
For example, we want cutouts to be done in a long-running process that pays the cost of importing a bunch of Python libraries just once during startup, not for each cutout.

Output
""""""

The output cutout should be a FITS image stored in the provided GCS bucket.
In the initial implementation, the backend produces only a FITS image.
Future versions may create other files, such as a metadata file for that image.
The cutout backend will return the GCS URLs of the newly-stored files.

The FITS file should contain metadata recording the input parameters, time at which the cutout was performed, and any other desirable provenance information.
(This can be postponed to a later revision of the backend.)

If the requested stencil extends outside the bounds of the image, it is clipped at the edges of the image and a cutout is returned for the clipped stencil (with no error reported).

Errors
""""""

If the stencil specifies an area with no overlap with the area covered by the image, an error should be reported.

Errors can be delivered in whatever form is easiest as long as the frontend can recover the details of the error.
(For example, an exception is fine as long as the user-helpful details of the error are in the exception.)

.. _cutout-future:

Future work
"""""""""""

We expect to add support for specifying the output image format and thus request a JPEG image (or whatever else makes sense).

In the future, we will probably support multiple ``ID`` parameters and possibly multiple stencils.
When supported, the semantics of multiple ``ID`` values and multiple stencils are combinatorial: in other words, the requested output is one cutout for each combination of ``ID`` and stencil.
So two ``ID`` values and a set of stencils consisting of two circles and one polygon would produce six cutouts: two circles and one polygon on both of the two ``ID`` values.

For cutouts with multiple ``ID`` parameters or multiple stencils, the current SODA standard requires that an error due to no overlap between the stencil and the image be handled by setting the corresponding result to a ``text/plain`` document starting with an error code.
This allows the error to be handled while still returning the other cutouts, but it seems unexpected and undesirable.

For these types of bulk cutouts, there is also some controversy currently over whether to return a single FITS file with HDUs for each cutout, or to return N separate FITS files.
The current SODA standard requires the latter, but the former may be easier to work with.
Because of this and the error handling problem discussed above, we may deviate from the SODA image cutout standard and define our own SODA operations that returns a single FITS file with improved error handling.

We will eventually need to support cutouts from virtual data products, which will not have UUIDs because they won't already be stored in the Butler.
A natural way of specifying such data products is the Butler data ID tuple.
When we add support for such cutouts, we expect to use a different input parameter or parameters to specify them, as an extension to the SODA protocol, rather than using ``ID``.

We may wish to support ``RANGE`` stencils in order to provide a more complete implementation of the SODA standard.

.. _results:

Results
=======

Result format
-------------

All cutout requests will create a FITS file.
A cutout request may also create additional output files if alternate image types are requested.
It may also create a separate metadata file.

The job representation for a successful async request in the initial implementation is a single FITS file with a result ID of ``cutout``.
The cutout image is stored as an extension in the FITS file, not in the Basic FITS HDU.
This output uses a ``Content-Type`` of ``application/fits`` [#]_.

.. [#] ``image/fits`` is not appropriate since no image is returned in the primary HDU.

The sync API redirects to the FITS file result of the underlying async job.

As discussed in :ref:`cutout-future`, there is some controversy over the output format when multiple ``ID`` parameters or stencils are provided.
The initial implementation only supports one ``ID`` parameter and one stencil.

The FITS file is provided to the user via a signed link for the location of the FITS file in the cutout object store.
Signed URLs are temporary and are expected to have a lifetime shorter than the job lifetime or the cutout object.
The initial implementation uses a signed URL lifetime of 15 minutes.
Therefore, the image cutout service generates new signed URLs each time the job results are requested.

The URL of the job result will therefore change, although the underlying objects will stay the same, and the client should not save the URL itself for later use.

The same approach will be used for other results, such as alternate image output formats, when those are supported.

The job record in Wobbly stores only the GCS URL to the cutout object store.
The conversion to a signed URL is done by the cutout API service as needed.

Alternate image types
"""""""""""""""""""""

.. note::

   This section describes future work that is not part of the initial implementation.

If another image type is requested, it will be returned alongside (not replacing) the FITS image.
If another image type is requested and multiple cutouts are requested via multiple stencil parameters, each converted cutout will be a separate entry in the result list for the job.
The converted images will be stored in the cutout object store alongside the FITS image.

If an alternate image type is requested, the order of results for the async job will list the converted images in the requested image type first, followed by the FITS file.
As with the FITS file, the images will be returned via signed links to the underlying object store.

The response to a sync request specifying an alternate image type will be a redirect to an object store link for the converted image of that type.
Sync requests that request an alternate image type must specify only one stencil parameter, since only one image can be returned via the sync API and the alternate image types we expect to support, unlike FITS, do not allow multiple images to be included in the same file. [#]_
This will be enforced by the service frontend.

.. [#] The result of a sync request with multiple stencils and an alternate image type could instead be a collection (such as a ZIP file) holding multiple images.
       However, this would mean the output MIME type of a sync request would depend on the number of stencil parameters, which is ugly.
       It would also introduce a new requirement for generating output collections that are not Butler collections.

       It is unlikely there will be a compelling need for a sync request for multiple cutouts with image conversion.
       That use case can use an async request instead.

Masking
-------

Due to the nature of common image formats, including FITS, the resulting cutout is forced to be rectangular.
However, the cutout stencil requested will often not be rectagular.

Ideally, the pixels required by the rectangular shape of the returned image but not requested by the cutout stencil would be masked out, allowing the client to (for example) do statistics on the returned image without having to account for data outside the requested range.
This will not be supported by the initial implementation due to performance problems with an early implementation.
(See DM-35020_ for more details.)

.. _DM-35020: https://jira.lsstcorp.org/browse/DM-35020

Support will hopefully be added in a later version.

This type of masking is not required by the IVOA SODA standard.

Result storage
--------------

The output cutout object store only retains files for a limited period of time to avoid unbounded storage requirements for cutouts that are no longer of interest.
The time at which the file will be deleted is advertised in the UWS job metadata via the destruction time parameter and is currently set to 30 days.

The object store is read-only for the users of the cutout service.

If the user who requested a cutout wishes to retain it, they should store the outputs in local storage, their home directory in the Rubin Science Platform, a personal Butler collection, or some other suitable location.

The SODA_ specification also allows a request to specify a VOSpace location in which to store the results, but does not specify a protocol for making that request.
The initial implementation of the image cutout service does not support this, but it may be considered in a future version.

Discovery
=========

The not-yet-written IVOA Registry service for the API Aspect of the Rubin Science Platform is out of scope for this document, except to note that the image cutout service will be registered there as a SODA service once the Registry service exists.

The identifiers returned in the ``obs_publisher_did`` column from ObsTAP queries in the Rubin Science Platform must be usable as ``ID`` parameter values for the image cutout service.

We run a DataLink_ service (currently implemented as the datalinker_ package) and reference it in the ``access_url`` column of ObsTAP queries.
That service provides links relevant to a specific result, including a DataLink service descriptor for the SODA-based cutout service.
This approach follows `section 4.2 of the SODA specification`_.

.. _DataLink: https://www.ivoa.net/documents/DataLink/20150617/REC-DataLink-1.0-20150617.html
.. _datalinker: https://github.com/lsst-sqre/datalinker
.. _section 4.2 of the SODA specification: https://www.ivoa.net/documents/SODA/20170517/REC-SODA-1.0.html#tth_sEc4.2

The initial implementation of this DataLink service descriptor does not provide information about the range of valid paramters for a cutout.
This will be added in a subsequent version.

Appendix: Options considered
============================

Below are design choices we considered when developing this approach.
This discussion is primarily of historical interest.

.. _uws-impl:

UWS implementations
-------------------

After a survey of available UWS implementations, we chose to write a new one on top of the Python Dramatiq_ distributed task queue.
We then later rewrote that implementation on top of arq_.

.. _Dramatiq: https://dramatiq.io/

UWS implementation survey
"""""""""""""""""""""""""

Since both the API frontend and the image cutout backend will be written in Python, a Python UWS implementation is desirable.
An implementation in a different language would require managing it as an additional stand-alone service that the API frontend would send jobs to, and then finding a way for it to execute Python code with those job parameters without access to Python libraries such as a Butler client.
We therefore ruled out UWS implementations in languages other than Python.

dax_imgserv_, the previous draft Rubin Observatory implementation of an image cutout service, which predates other design discussions discussed here, contains the skeleton of a Python UWS implementation built on Celery_ and Redis_.
However, job tracking was not yet implemented.

.. _dax_imgserv: https://github.com/lsst/dax_imgserv/
.. _Celery: https://docs.celeryproject.org/en/stable/index.html

uws-api-server_ is a more complete UWS implementation that uses Kubernetes as the task execution system and as the state tracking repository for jobs.
This is a clever approach that minimizes the need for additional dependencies, but it requires creating a Kubernetes ``Job`` resource per processing task.
The resulting overhead of container creation is expected to be prohibitive for the performance and throughput constraints required for the image cutout service.
This implementation also requires a shared POSIX file system for storage of results, but an object store that supports automatic object expiration is a more natural choice for time-bounded cutout storage and for objects that must be returned via a REST API.
Finally, tracking of completed jobs in this approach is vulnerable to the vagaries of Kubernetes retention of metadata for completed jobs, which may not be sufficiently flexible for our needs.

.. _uws-api-server: https://github.com/lsst-dm/uws-api-server

We did not find any other re-usable Python UWS server implementations (as opposed to clients, of which there are several).

Task queue options
""""""""""""""""""

Celery_ is the standard Python task queuing system, so it was our default choice unless a different task queue system looked compelling.
However, Dramatiq_ appeared to have some advantages over Celery, and there were multiple reports of other teams who had switched to Dramatiq from Celery due to instability issues and other frustration.

Both frameworks were similar, so switching between them if necessary seemed like it would not be default.
Compared to Celery, Dramatiq offered per-task prioritization without creating separate priority workers.
We expect to do a lot of task prioritization to support sync requests, deprioritize expensive requests, throttle requests when the cluster is overloaded, and for other reasons, so this was appealing.
Dramatiq is also smaller and simpler, which is always a minor advantage.

One concern we had with Dramatiq is that it's a younger project primarily written by a single developer.
Celery is the standard task queue system for Python, so it is likely to continue to be supported well into the future.
There was some increased risk with Dramatiq that it would be abandoned and we will need to replace it later.
However, it appears to have growing popularity and some major corporate users, which is reassuring.

Dramatiq supports either Redis_, RabbitMQ_, or Amazon SQS as the underlying message bus.
Both Dramatiq and Celery prefer RabbitMQ and the Celery documentation warns that Redis can lose data in some unclean shutdown scenarios.
However, we are already using Redis as a component of the Rubin Science Platform underlying multiple services, so we chose to use Redis as the message bus to avoid adding a new infrastructure component until this is shown to be a reliability issue.

.. _RabbitMQ: https://www.rabbitmq.com/

Dramatiq supports either Redis or Memcache as a store for task results.
We only needed very temporary task result storage to handle storing job results in the database, and are already using Redis for the message bus, so we used Redis for task result storage as well.

Neither Celery nor Dramatiq support asyncio natively.
Dramatiq is unlikely to add support since the maintainer `is not a fan of asyncio <https://github.com/Bogdanp/dramatiq/issues/238>`__.
Initially, therefore, we enqueued tasks synchronously.

After completing an initial implementation using Dramatiq, we discovered arq_, which has the substantial advantage of supporting asyncio.
We therefore chose to rewrite the cutout service on top of arq, eliminating the awkwardness of synchronous Redis calls in the frontend.

The main feature of Dramatiq that was lost in this rewrite was task priorities.
arq does not support setting priorities on tasks or reordering tasks based on priority.
So far, we have not used priorities in the cutout service, but this may be a problem if, as anticipated, we need them for throttling and prioritization of synchronous requests.

.. _task-storage:

Task result storage
"""""""""""""""""""

An image cutout task produces two types of output: the cutouts themselves with their associated astronomical metadata, and the metadata about the request.
The latter includes the parameters of the cutout request, the job status, and any error messages.

The task queuing system would appear to be the natural store for the task metadata.
However, even with a configured result store, the task queuing system only stores task metadata while the task is running and for a short time afterwards.
The intent of the task system is for the invoker of the task to ask for the results, at which point they are delivered and then discarded.

The internal result storage is also intended for small amounts of serializable data, not for full image cutouts.
The natural data store for image cutouts is an object store.

Therefore, each worker task takes responsibility for storing the cutout results in an external store.
Only pointers to that external store are stored in the internal result storage, and only for long enough to record them in a database.

The task metadata (success or failure, any error message, pointers to the result store, the request parameters, and the other metadata for a job required by the UWS specification) is stored in a SQL database external to the task queue system.
The parameters known before job execution (such as the request parameters) is stored by the frontend.
The other data is stored by job queue workers via callbacks triggered by the success or failure of the cutout worker.
The image cutout web service uses the SQL database to retrieve information about finished jobs.
It can ask the task queuing system for information about still-running jobs that have not yet stored their result metadata, although in the initial implementation it only uses the database for that information.
This will satisfy the UWS API requirements.
