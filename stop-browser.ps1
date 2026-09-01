$connection = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($connection) {
    $connection | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        Stop-Process -Id $_ -ErrorAction Stop
    }
}
