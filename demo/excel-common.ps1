# Shared helpers for the hidden-Excel demo scripts. Dot-source this file.
# New-HiddenExcel starts a private Excel instance; Close-HiddenExcel quits it and, if it lingers, stops only that
# process (its PID comes from its own window handle), never Matt's Excel.

if (-not ('ComRetry.Filter' -as [type])) {
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
namespace ComRetry {
  [ComImport, Guid("00000016-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IOleMessageFilter {
    [PreserveSig] int HandleInComingCall(int dwCallType, IntPtr hTaskCaller, int dwTickCount, IntPtr lpInterfaceInfo);
    [PreserveSig] int RetryRejectedCall(IntPtr hTaskCallee, int dwTickCount, int dwRejectType);
    [PreserveSig] int MessagePending(IntPtr hTaskCallee, int dwTickCount, int dwPendingType); }
  public class Filter : IOleMessageFilter {
    [DllImport("Ole32.dll")] static extern int CoRegisterMessageFilter(IOleMessageFilter n, out IOleMessageFilter o);
    [DllImport("user32.dll")] static extern int GetWindowThreadProcessId(IntPtr h, out int p);
    public static void Register() { IOleMessageFilter o; CoRegisterMessageFilter(new Filter(), out o); }
    public static int PidOf(int hwnd) { int p; GetWindowThreadProcessId(new IntPtr(hwnd), out p); return p; }
    int IOleMessageFilter.HandleInComingCall(int a, IntPtr b, int c, IntPtr d) { return 0; }
    int IOleMessageFilter.RetryRejectedCall(IntPtr a, int tick, int type) { return (type == 2 && tick < 600000) ? 250 : -1; }
    int IOleMessageFilter.MessagePending(IntPtr a, int b, int c) { return 2; } } }
'@
}
[ComRetry.Filter]::Register()

function New-HiddenExcel {
  $before = @(Get-Process EXCEL -ErrorAction SilentlyContinue | ForEach-Object Id)
  $xl = New-Object -ComObject Excel.Application
  $xl.Visible = $false; $xl.DisplayAlerts = $false; $xl.AutomationSecurity = 3
  $p = 0; try { $p = [ComRetry.Filter]::PidOf([int]$xl.Hwnd) } catch {}
  if ($p -le 0) {
    # fallback: the one EXCEL process that appeared during New-Object (only if exactly one did)
    $new = @(Get-Process EXCEL -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id })
    if ($new.Count -eq 1) { $p = $new[0].Id }
  }
  $script:HiddenExcelPid = $p
  return $xl
}

function Close-HiddenExcel($xl) {
  $p = $script:HiddenExcelPid
  try { $xl.Quit() } catch {}
  [Runtime.InteropServices.Marshal]::ReleaseComObject($xl) | Out-Null
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
  if ($p -gt 0) {
    for ($i = 0; $i -lt 10 -and (Get-Process -Id $p -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
    Get-Process -Id $p -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'EXCEL' -and $_.MainWindowTitle -eq '' } | Stop-Process -Force
    if ($env:XL_DEBUG) { "closed hidden Excel pid $p; still running: $([bool](Get-Process -Id $p -ErrorAction SilentlyContinue))" }
  } elseif ($env:XL_DEBUG) { 'hidden Excel pid unknown; not stopped' }
}
