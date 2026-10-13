@echo off
rem Builds ..\StartJobSearch.exe with the C# compiler that ships with Windows (.NET Framework 4).
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
"%CSC%" /nologo /target:winexe /out:"%~dp0..\StartJobSearch.exe" ^
  /reference:System.Web.Extensions.dll /reference:System.Windows.Forms.dll ^
  "%~dp0StartJobSearch.cs"
if errorlevel 1 (echo Build FAILED & exit /b 1)
echo Built %~dp0..\StartJobSearch.exe
