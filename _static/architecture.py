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

    metadata = SQL("UWS database")
    butler = Datastore("Butler repository")
    datastore = Datastore("Object store")

    with Cluster("Kubernetes"):
        ingress = LoadBalancing("NGINX ingress")
        gafaelfawr = KubernetesEngine("Gafaelfawr")

        with Cluster("Cutout service"):
            api = KubernetesEngine("API service")
            cutout_workers = KubernetesEngine("Workers (stack)")
            uws_workers = KubernetesEngine("Workers (database)")
            redis = PersistentDisk("Redis")

    user >> ingress >> api >> Edge(label="Dramatiq") >> redis
    api - metadata << uws_workers
    ingress >> Edge(label="Auth request") >> gafaelfawr
    redis - Edge(label="Dramatiq") - cutout_workers >> butler >> api
    redis >> uws_workers
    butler >> datastore
    user << datastore
