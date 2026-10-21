<?php
// SPDX-License-Identifier: Apache-2.0
// Trusted parent-side observer. Candidate processes never receive admin credentials.
$input = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
function connectFixture(string $url): PDO {
    if (str_starts_with($url, 'sqlite:///')) {
        $pdo = new PDO('sqlite:' . substr($url, strlen('sqlite:///')));
        $pdo->exec('PRAGMA foreign_keys=ON');
    } else {
        $parts = parse_url($url);
        $pdo = new PDO('mysql:host=' . $parts['host'] . ';port=' . ($parts['port'] ?? 3306)
            . ';dbname=' . ltrim($parts['path'], '/') . ';charset=utf8mb4',
            rawurldecode($parts['user']), rawurldecode($parts['pass'] ?? ''));
        $pdo->exec('SET SESSION information_schema_stats_expiry=0');
    }
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);
    $pdo->setAttribute(PDO::ATTR_EMULATE_PREPARES, false);
    $pdo->setAttribute(PDO::ATTR_STRINGIFY_FETCHES, false);
    return $pdo;
}
try {
    $pdo = connectFixture($input['url']);
    if ($input['action'] === 'provision' || $input['action'] === 'drop') {
        $name = $input['name'];
        if (!preg_match('/^sanka_php_[st]_[a-f0-9]{12}$/D', $name)) {
            throw new RuntimeException('invalid disposable name');
        }
        if ($input['action'] === 'provision') {
            $pdo->exec('CREATE DATABASE `' . $name . '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci');
            $pdo->exec('CREATE USER ' . $pdo->quote($name) . "@'%' IDENTIFIED BY " . $pdo->quote($input['password']));
            $pdo->exec('GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON `' . $name . '`.* TO ' . $pdo->quote($name) . "@'%'");
        } else {
            $pdo->exec('DROP DATABASE IF EXISTS `' . $name . '`');
            $pdo->exec('DROP USER IF EXISTS ' . $pdo->quote($name) . "@'%'");
        }
        echo json_encode(['ok'=>true]);
    } elseif ($input['action'] === 'schema') {
        foreach ($input['models'] as $model) { $pdo->exec($model['statement']); }
        echo json_encode(['ok'=>true]);
    } elseif ($input['action'] === 'snapshot') {
        $tables = []; $sequences = [];
        foreach ($input['models'] as $model) {
            $table = $model['name']; $primary = $model['primary_key'];
            if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/D', $table)
                    || !preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/D', $primary)) {
                throw new RuntimeException('invalid table');
            }
            $tables[$table] = $pdo->query('SELECT * FROM `' . $table . '` ORDER BY `' . $primary . '`')->fetchAll();
            if ($model['auto']) {
                if (str_starts_with($input['url'], 'sqlite:///')) {
                    $query = $pdo->prepare('SELECT seq FROM sqlite_sequence WHERE name=?');
                } else {
                    $query = $pdo->prepare('SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?');
                }
                $query->execute([$table]);
                $value = $query->fetchColumn();
                $sequences[$table] = $value === false || $value === null ? null : (int)$value;
            }
        }
        echo json_encode(['tables'=>(object)$tables, 'sequences'=>(object)$sequences], JSON_THROW_ON_ERROR);
    } else { throw new RuntimeException('unsupported observer action'); }
} catch (Throwable $error) {
    fwrite(STDERR, 'Disposable database operation failed.');
    exit(1);
}
