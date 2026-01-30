#!/bin/bash

#Install bats
echo "Installing Bats before test execution"
npm install bats@1.13.0
npm install bats-mock@1.2.5

#Run tests
./node_modules/.bin/bats ${APP_PATH}/tests/*.bats
