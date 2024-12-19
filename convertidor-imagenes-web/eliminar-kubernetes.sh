#!/bin/bash

kubectl delete service traefik -n convertidor-imagenes

kubectl delete -f manifests/ -n convertidor-imagenes
