<?php
// SPDX-License-Identifier: Apache-2.0
function health(array $request) {
    return ['status' => 200, 'body' => ['message' => 'pong', 'max_integer' => 9223372036854775807]];
}
function greet(array $request) {
    $name = $request['query']['name'] ?? 'world';
    return ['status' => 200, 'body' => ['id' => $request['params']['id'], 'name' => $name,
        'truthy' => !!$name, 'is_integer' => $name === 0]];
}
function echo_payload(array $request) {
    if (($request['headers']['authorization'] ?? '') !== 'Bearer sanka-fixture-token') {
        return ['status' => 401, 'body' => ['message' => 'Unauthorized']];
    }
    return ['status' => 200, 'body' => $request['body']];
}
