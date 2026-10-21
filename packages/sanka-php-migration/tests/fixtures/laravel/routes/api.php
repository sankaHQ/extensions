<?php
// SPDX-License-Identifier: Apache-2.0
use Illuminate\Support\Facades\Route;
use Illuminate\Support\Facades\DB;
use Illuminate\Http\Request;

Route::get('/orders', function (Request $request) {
    $owner = 'alice';
    if ($request->bearerToken() !== env('ALICE_API_TOKEN')) {
        if ($request->bearerToken() !== env('BOB_API_TOKEN')) {
            return response()->json(['message' => 'Unauthorized'], 401);
        }
        $owner = 'bob';
    }
    return response()->json(DB::select('SELECT id, name, amount, owner FROM orders WHERE owner = ? ORDER BY id', [$owner]));
});

Route::get('/orders/{id}', function (Request $request, $id) {
    $owner = 'alice';
    if ($request->bearerToken() !== env('ALICE_API_TOKEN')) {
        if ($request->bearerToken() !== env('BOB_API_TOKEN')) {
            return response()->json(['message' => 'Unauthorized'], 401);
        }
        $owner = 'bob';
    }
    $rows = DB::select('SELECT id, name, amount, owner FROM orders WHERE id = ? AND owner = ?', [$id, $owner]);
    if ($rows === []) { return response()->json(['message' => 'Not Found'], 404); }
    return response()->json($rows[0]);
});

Route::post('/orders', function (Request $request) {
    $owner = 'alice';
    if ($request->bearerToken() !== env('ALICE_API_TOKEN')) {
        if ($request->bearerToken() !== env('BOB_API_TOKEN')) {
            return response()->json(['message' => 'Unauthorized'], 401);
        }
        $owner = 'bob';
    }
    $data = $request->validate(['name' => 'required|string|max:80', 'amount' => 'required|string|max:12']);
    return DB::transaction(function () use ($data, $owner, $request) {
        DB::insert('INSERT INTO orders (name, amount, owner) VALUES (?, ?, ?)', [$data['name'], $data['amount'], $owner]);
        if ($request->query('fail') === 'yes') { throw new RuntimeException('synthetic rollback'); }
        $rows = DB::select('SELECT id, name, amount, owner FROM orders WHERE owner = ? ORDER BY id DESC LIMIT 1', [$owner]);
        return response()->json($rows[0], 201);
    });
});

Route::patch('/orders/{id}', function (Request $request, $id) {
    $owner = 'alice';
    if ($request->bearerToken() !== env('ALICE_API_TOKEN')) {
        if ($request->bearerToken() !== env('BOB_API_TOKEN')) {
            return response()->json(['message' => 'Unauthorized'], 401);
        }
        $owner = 'bob';
    }
    $data = $request->validate(['name' => 'required|string|max:80']);
    $changed = DB::update('UPDATE orders SET name = ? WHERE id = ? AND owner = ?', [$data['name'], $id, $owner]);
    if ($changed === 0) { return response()->json(['message' => 'Not Found'], 404); }
    return response()->json(['updated' => true]);
});

Route::delete('/orders/{id}', function (Request $request, $id) {
    $owner = 'alice';
    if ($request->bearerToken() !== env('ALICE_API_TOKEN')) {
        if ($request->bearerToken() !== env('BOB_API_TOKEN')) {
            return response()->json(['message' => 'Unauthorized'], 401);
        }
        $owner = 'bob';
    }
    $changed = DB::delete('DELETE FROM orders WHERE id = ? AND owner = ?', [$id, $owner]);
    if ($changed === 0) { return response()->json(['message' => 'Not Found'], 404); }
    return response()->json(['deleted' => true]);
});
