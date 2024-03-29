#!/bin/sh
set -o errexit

# 1. Create registry container unless it already exists
reg_name='kind-registry'
reg_port='5001'

if [ "$(docker inspect -f '{{.State.Running}}' "${reg_name}" 2>/dev/null || true)" != 'true' ]; then
  docker run \
    -d --restart=always -p "127.0.0.1:${reg_port}:5000" --name "${reg_name}" \
    registry:2
fi

# 2. Create kind cluster with containerd registry config dir enabled
cluster_name='aircube'
kind create cluster --name "${cluster_name}" --config scripts/kind-cluster.yaml

# 3. Add the registry config to the nodes
# This is necessary because localhost resolves to loopback addresses that are network-namespace local.
# In other words: localhost in the container is not localhost on the host.
# We want a consistent name that works from both ends, so we tell containerd to
# alias localhost:${reg_port} to the registry container when pulling images
REGISTRY_DIR="/etc/containerd/certs.d/localhost:${reg_port}"
for node in $(kind get nodes); do
  docker exec "${node}" mkdir -p "${REGISTRY_DIR}"
  cat <<EOF | docker exec -i "${node}" cp /dev/stdin "${REGISTRY_DIR}/hosts.toml"
[host."http://${reg_name}:5000"]
EOF
done

# 4. Connect the registry to the cluster network if not already connected
# This allows kind to bootstrap the network but ensures they're on the same network
if [ "$(docker inspect -f='{{json .NetworkSettings.Networks.kind}}' "${reg_name}")" = 'null' ]; then
  docker network connect "kind" "${reg_name}"
fi

# 5. Document the local registry
# https://github.com/kubernetes/enhancements/tree/master/keps/sig-cluster-lifecycle/generic/1755-communicating-a-local-registry
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${reg_port}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

# 6. Create GCP service account
sa_file_path=files/serviceaccount.json
kubectl create ns airflow
kubectl create secret generic airflow-gcp-sa --from-file=${sa_file_path} -n airflow

# 7. Create Git SSH key
ssh_key_file_path=files/gitSshKey
kubectl create secret generic airflow-ssh-secret --from-file=${ssh_key_file_path} -n airflow

# 8. Create git-credentials
kubectl create secret generic git-credentials --from-file=GIT_SYNC_USERNAME=files/GIT_SYNC_USERNAME --from-file=GIT_SYNC_PASSWORD=files/GIT_SYNC_PASSWORD -n airflow
