"""Source for the architecture diagram."""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.compute import KubernetesEngine
from diagrams.gcp.database import Datastore, SQL
from diagrams.gcp.network import LoadBalancing
from diagrams.gcp.storage import PersistentDisk
from diagrams.onprem.client import User

graph_attr = {
    "label": "",
    "nodesep": "0.2",
    "pad": "0.2",
    "ranksep": "0.75",
    "splines": "spline",
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

    images = Datastore("Image store")
    datastore = Datastore("Cutout store")

    with Cluster("Kubernetes"):
        ingress = LoadBalancing("NGINX ingress")
        gafaelfawr = KubernetesEngine("Gafaelfawr")
        butler = KubernetesEngine("Butler API")

        with Cluster("Cutout service"):
            api = KubernetesEngine("API service")
            cutout_workers = KubernetesEngine("Workers (stack)")
            uws_workers = KubernetesEngine("Workers (database)")
            redis = PersistentDisk("Redis")

        with Cluster("Wobbly"):
            wobbly = KubernetesEngine("API")
            metadata = SQL("UWS database")

    user >> ingress >> api
    api - Edge(label="arq") - redis
    api >> wobbly << uws_workers
    wobbly >> metadata
    ingress >> gafaelfawr
    redis - Edge(label="arq") - cutout_workers >> datastore
    cutout_workers << butler << images
    redis - Edge(label="arq") - uws_workers
    user << datastore
