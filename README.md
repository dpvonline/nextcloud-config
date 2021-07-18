# DPV Nextcloud Config

This is a short description of the contents of this repository. A full German description is 
available as the "DPV Cloud Handbuch". 

## Setup description
This configuration contains two profiles: One for local development and one for DPV Cloud 
production. The both share the following containers: 

- db: maria database server
- redis: redis cache
- app: nextcloud container 
- proxy: nginx container 
- backup: custom backup script

## Installation

### Local setup
With the following command you can pull and build all containers: 
```
docker-compose -f docker-compose.yml -f docker-compose.local.yml build --pull
```
Make sure to fill all the required values in the *.env files before continuing. 
Start the microservices with: 
```
docker-compose -f docker-compose.yml -f docker-compose.local.yml up
```
This will show you all the logs in your current terminal, which is probably a good 
idea for first start. If you want to run in background, just append `-d` or `--detach`.

### Production setup
```
docker-compose -f docker-compose.yml -f docker-compose.prod.yml build --pull
```
Make sure to fill all the required values in the *.env files before continuing.
Start the microservices with:
```
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up
```
This will show you all the logs in your current terminal, which is probably a good
idea for first start. If you want to run in background, just append `-d` or `--detach`.


If you want to run with office package enabled, use: 
```
docker-compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.office.yml up -d
```

## Installation hints

### Web discovery

In order to fix web discovery manually change in .htaccess: 
```
RewriteRule ^\.well-known/carddav /remote.php/dav/ [R=301,L]
RewriteRule ^\.well-known/caldav /remote.php/dav/ [R=301,L]
```
to 
```
RewriteRule ^\.well-known/carddav https://%{SERVER_NAME}/remote.php/dav/ [R=301,L]
RewriteRule ^\.well-known/caldav https://%{SERVER_NAME}/remote.php/dav/ [R=301,L]
```