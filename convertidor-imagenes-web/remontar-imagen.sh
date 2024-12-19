#!/bin/bash

kubectl delete service traefik -n convertidor-imagenes

kubectl delete -f manifests/ -n convertidor-imagenes

kubectl apply -f https://raw.githubusercontent.com/traefik/traefik/refs/heads/v2.10/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml

echo "Waiting for Traefik CRDs to be established..."
sleep 2

kubectl apply -f <(curl -s https://raw.githubusercontent.com/traefik/traefik/v2.10/docs/content/reference/dynamic-configuration/kubernetes-crd-rbac.yml | sed 's/namespace: default/namespace: convertidor-imagenes/')

echo "Building and pushing Docker image..."
docker build -t dmolmar/images-api:latest .
docker push dmolmar/images-api:latest

echo "Applying new manifests..."
kubectl apply -f manifests/ -n convertidor-imagenes

echo "Deployment complete."
