#!/bin/bash

# Delete traefik service to re-create it
kubectl delete service traefik -n convertidor-imagenes

# Delete old resources, including Traefik, worker and images-api deployments
kubectl delete -f manifests/ -n convertidor-imagenes

# Apply Traefik CRDs separately (without namespace flag)
kubectl apply -f https://raw.githubusercontent.com/traefik/traefik/refs/heads/v2.10/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml

# Wait for CRDs to be established (optional, increase the time if you still have the same issue)
echo "Waiting for Traefik CRDs to be established..."
sleep 2

# Apply Traefik RBAC (corrected from URL)
kubectl apply -f <(curl -s https://raw.githubusercontent.com/traefik/traefik/v2.10/docs/content/reference/dynamic-configuration/kubernetes-crd-rbac.yml | sed 's/namespace: default/namespace: convertidor-imagenes/')

# Build and push the new Docker image
docker build -t dmolmar/images-api:latest .
docker push dmolmar/images-api:latest

# Apply the new manifests, including Traefik (now with namespaces specified)
kubectl apply -f manifests/ -n convertidor-imagenes

echo "Deployment complete."
