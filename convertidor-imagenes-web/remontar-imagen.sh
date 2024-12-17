kubectl delete -f manifests/ -n convertidor-imagenes
docker build -t dmolmar/images-api:latest .
docker push dmolmar/images-api:latest
kubectl apply -f manifests/ -n convertidor-imagenes
