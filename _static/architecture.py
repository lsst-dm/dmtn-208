"""Source for architecture.png, the architecture diagram."""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.compute import KubernetesEngine
from diagrams.gcp.database import Datastore, SQL
from diagrams.gcp.network import LoadBalancing
from diagrams.gcp.storage import PersistentDisk
from diagrams.onprem.client import User

os.chdir(os.path.dirname(__file__))

graph_attr = {
    "label": "",
    "nodesep": "0.2",
    "pad": "0.2",
    "ranksep": "0.75",
}

node_attr = {
    "fontsize": "14.0",
}

edge_attr = {
    "fontsize": "10.0",
}

with Diagram(
    "Image cutout service",
    show=False,
    filename="architecture",
    outformat="png",
    graph_attr=graph_attr,
    node_attr=node_attr,
):
    user = User("End user")

    butler = Datastore("Butler repository")
    images = Datastore("Image store")
    datastore = Datastore("Cutout store")

    with Cluster("Kubernetes"):
        ingress = LoadBalancing("NGINX ingress")
        gafaelfawr = KubernetesEngine("Gafaelfawr")
        metadata = SQL("UWS database")

        with Cluster("Cutout service"):
            api = KubernetesEngine("API service")
            cutout_workers = KubernetesEngine("Workers (stack)")
            uws_workers = KubernetesEngine("Workers (database)")
            redis = PersistentDisk("Redis")

    user >> ingress >> api
    api - Edge(label="Dramatiq") - redis
    api - metadata << uws_workers
    ingress >> gafaelfawr
    redis - Edge(label="Dramatiq") - cutout_workers >> datastore >> api
    cutout_workers >> butler >> images
    redis - Edge(label="Dramatiq") - uws_workers
    user << datastore
