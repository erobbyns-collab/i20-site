<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="2.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:sitemap="http://www.sitemaps.org/schemas/sitemap/0.9">
<xsl:output method="html" encoding="UTF-8" indent="yes"/>
<xsl:template match="/">
<html lang="en-GB">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Sitemap — i20</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin=""/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&amp;family=Fraunces:opsz,wght@9..144,700&amp;display=swap" rel="stylesheet"/>
  <style>
    :root{--bg:#f0f1f3;--card:#fff;--text:#1a1a2e;--muted:#8a8a9e;--border:#e2e3e8;--accent:#1e3a5f}
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:var(--text);font-family:"Inter",-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
    .wrap{max-width:820px;margin:0 auto;padding:3rem 1.5rem 4rem}
    h1{font-family:"Fraunces",Georgia,serif;font-size:1.6rem;font-weight:700;letter-spacing:-.02em;margin-bottom:.25rem}
    .subtitle{color:var(--muted);font-size:.85rem;margin-bottom:2rem}
    .count{display:inline-block;background:var(--accent);color:#fff;font-size:.7rem;font-weight:600;padding:.15rem .55rem;border-radius:4px;margin-left:.5rem;vertical-align:middle}
    table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
    thead{background:var(--accent)}
    th{color:#fff;font-size:.7rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;text-align:left;padding:.75rem 1rem}
    td{padding:.65rem 1rem;border-top:1px solid var(--border);font-size:.85rem}
    tr:hover td{background:#f8f8fa}
    td a{color:var(--accent);text-decoration:none;word-break:break-all}
    td a:hover{text-decoration:underline}
    .priority{text-align:center}
    .date{color:var(--muted);white-space:nowrap}
    .back{display:inline-block;font-size:.8rem;font-weight:600;letter-spacing:.06em;color:var(--muted);text-decoration:none;margin-bottom:2rem}
    .back:hover{color:var(--text)}
    @media(max-width:600px){th,td{padding:.5rem .6rem;font-size:.78rem}}
  </style>
</head>
<body>
  <div class="wrap">
    <a href="/" class="back">&#8592; Back to i20</a>
    <h1>Sitemap<span class="count"><xsl:value-of select="count(sitemap:urlset/sitemap:url)"/> pages</span></h1>
    <p class="subtitle">All pages on i20.co.uk, indexed weekly for search engines.</p>
    <table>
      <thead>
        <tr>
          <th>URL</th>
          <th>Priority</th>
          <th>Last updated</th>
        </tr>
      </thead>
      <tbody>
        <xsl:for-each select="sitemap:urlset/sitemap:url">
          <tr>
            <td><a href="{sitemap:loc}"><xsl:value-of select="sitemap:loc"/></a></td>
            <td class="priority"><xsl:value-of select="sitemap:priority"/></td>
            <td class="date"><xsl:value-of select="sitemap:lastmod"/></td>
          </tr>
        </xsl:for-each>
      </tbody>
    </table>
  </div>
</body>
</html>
</xsl:template>
</xsl:stylesheet>
