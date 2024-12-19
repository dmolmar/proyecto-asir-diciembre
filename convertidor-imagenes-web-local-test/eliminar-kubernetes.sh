#!/bin/bash

# Delete traefik service to re-create it
kubectl delete service traefik -n convertidor-imagenes

# Delete old resources, including Traefik, worker and images-api deployments
kubectl delete -f manifests/ -n convertidor-imagenes
