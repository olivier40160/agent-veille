/**
 * Point d'entrée Webhook POST pour l'Agent de Veille & Éditorial.
 * Toutes les clés et identifiants Drive sont lus depuis PropertiesService.getScriptProperties().
 */
function doPost(e) {
  var logs = [];
  function log(msg) {
    var entry = new Date().toISOString() + " : " + msg;
    logs.push(entry);
    Logger.log(msg);
  }

  function jsonResponse(payloadObj) {
    return ContentService.createTextOutput(JSON.stringify(payloadObj))
      .setMimeType(ContentService.MimeType.JSON);
  }

  try {
    // 1. Contrôle de présence et de taille maximale du payload (2 Mo max)
    if (!e || !e.postData || typeof e.postData.contents !== "string" || e.postData.contents.length === 0) {
      return jsonResponse({
        "status": "error",
        "message": "Payload vide ou invalide"
      });
    }
    if (e.postData.contents.length > 2 * 1024 * 1024) {
      return jsonResponse({
        "status": "error",
        "message": "Payload trop volumineux (max 2 Mo)"
      });
    }

    // 2. Lecture des secrets et IDs depuis les Propriétés du script
    var props = PropertiesService.getScriptProperties();
    var SECRET_KEY = props.getProperty("WEBHOOK_SECRET");
    var MEMORY_FILE_ID = props.getProperty("MEMORY_FILE_ID");
    var ARCHIVE_FOLDER_ID = props.getProperty("ARCHIVE_FOLDER_ID");
    var MAIN_DOC_ID = props.getProperty("MAIN_DOC_ID");
    var EDITORIEL_ARCHIVE_FOLDER_ID = props.getProperty("EDITORIEL_ARCHIVE_FOLDER_ID");

    if (!SECRET_KEY) {
      log("Erreur critique : WEBHOOK_SECRET absent des ScriptProperties.");
      return jsonResponse({
        "status": "error",
        "message": "Configuration serveur incomplète"
      });
    }

    log("Début exécution doPost");
    var data = JSON.parse(e.postData.contents);

    if (!data || typeof data.secret !== "string" || data.secret !== SECRET_KEY) {
      log("Erreur : Secret invalide");
      return jsonResponse({
        "status": "error",
        "message": "Non autorisé",
        "logs": logs
      });
    }

    // ==========================================
    // BRANCHE 1 : LECTURE DE LA MÉMOIRE DRIVE
    // ==========================================
    if (data.action === "get_memory") {
      log("Action reçue : LECTURE MÉMOIRE DRIVE");
      try {
        if (!MEMORY_FILE_ID) throw new Error("Propriété MEMORY_FILE_ID non configurée.");
        var memFile = DriveApp.getFileById(MEMORY_FILE_ID);
        var jsonText = memFile.getBlob().getDataAsString("UTF-8");
        log("Mémoire lue avec succès depuis Google Drive.");
        return jsonResponse({
          "status": "success",
          "data": JSON.parse(jsonText),
          "logs": logs
        });
      } catch (memReadErr) {
        log("ERREUR lecture mémoire : " + memReadErr.toString());
        return jsonResponse({
          "status": "error",
          "message": memReadErr.toString(),
          "logs": logs
        });
      }
    }

    // ==========================================
    // BRANCHE 2 : SAUVEGARDE DE LA MÉMOIRE DRIVE
    // ==========================================
    if (data.action === "save_memory") {
      log("Action reçue : SAUVEGARDE MÉMOIRE DRIVE");
      try {
        if (!MEMORY_FILE_ID) throw new Error("Propriété MEMORY_FILE_ID non configurée.");
        if (typeof data.memory_data !== "object" || data.memory_data === null) {
          throw new Error("Paramètre memory_data invalide.");
        }
        var memFile = DriveApp.getFileById(MEMORY_FILE_ID);
        memFile.setContent(JSON.stringify(data.memory_data, null, 2));
        log("Mémoire sauvegardée avec succès sur Google Drive.");
        return jsonResponse({
          "status": "success",
          "message": "Mémoire mise à jour",
          "logs": logs
        });
      } catch (memWriteErr) {
        log("ERREUR sauvegarde mémoire : " + memWriteErr.toString());
        return jsonResponse({
          "status": "error",
          "message": memWriteErr.toString(),
          "logs": logs
        });
      }
    }

    // ==========================================
    // BRANCHE 4 : LECTURE DU DERNIER RAPPORT BRUT (ÉDITORIAL)
    // ==========================================
    if (data.action === "get_latest_report") {
      log("Action reçue : LECTURE DU DERNIER RAPPORT BRUT");
      try {
        if (!MAIN_DOC_ID) throw new Error("Propriété MAIN_DOC_ID non configurée.");
        var mainDoc = DocumentApp.openById(MAIN_DOC_ID);
        var text = mainDoc.getBody().getText();

        // Exclusion automatique du bloc purement technique/logiciel pour les personas éditoriaux
        var titreAExclure = "Veille Logicielle, Releases & Incidents Majeurs (Ops)";
        var indexExclu = text.indexOf(titreAExclure);
        if (indexExclu !== -1) {
          text = text.substring(0, indexExclu).trim();
          log("Section technique '" + titreAExclure + "' exclue du flux éditorial.");
        }

        log("Rapport brut lu avec succès (" + text.length + " caractères).");
        return jsonResponse({
          "status": "success",
          "content": text,
          "logs": logs
        });
      } catch (readReportErr) {
        log("ERREUR lecture rapport brut : " + readReportErr.toString());
        return jsonResponse({
          "status": "error",
          "message": readReportErr.toString(),
          "logs": logs
        });
      }
    }

    // ==========================================
    // BRANCHE 5 : PUBLICATION ET ARCHIVAGE ÉDITORIAL PAR JOUR
    // ==========================================
    if (data.action === "publish_editoriel") {
      log("Action reçue : PUBLICATION RAPPORT ÉDITORIAL");
      try {
        if (typeof data.content !== "string" || data.content.trim() === "") {
          throw new Error("Contenu éditorial vide ou invalide.");
        }
        if (!EDITORIEL_ARCHIVE_FOLDER_ID) {
          throw new Error("Propriété EDITORIEL_ARCHIVE_FOLDER_ID non configurée.");
        }

        var JOURS_VALIDES = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "SAMEDI", "DIMANCHE"];
        var jour = (typeof data.jour === "string" ? data.jour : "DIMANCHE").toUpperCase().trim();
        if (JOURS_VALIDES.indexOf(jour) === -1) {
          jour = "DIMANCHE";
        }

        var targetDocId = props.getProperty("DOC_EDITORIAL_" + jour) || props.getProperty("DOC_EDITORIAL_DIMANCHE");
        if (!targetDocId) {
          throw new Error("Propriété DOC_EDITORIAL_" + jour + " non configurée.");
        }

        log("Publication ciblée sur le document du jour [" + jour + "]");

        var nowStr = new Date().toLocaleString("fr-FR", { timeZone: "Europe/Paris" });
        var dateFileStr = formatTimestampForFilename(new Date());

        // 1. Mise à jour du document éditorial du jour
        updateDocumentContent(targetDocId, data.content, nowStr);
        log("Document éditorial [" + jour + "] mis à jour avec succès.");

        // 2. Copie directe du document formaté vers le dossier d'archive
        log("Création de l'archive 'Editoriel_" + jour + "_" + dateFileStr + "' par copie directe...");
        var targetFolder = DriveApp.getFolderById(EDITORIEL_ARCHIVE_FOLDER_ID);
        var sourceFile = DriveApp.getFileById(targetDocId);
        sourceFile.makeCopy("Editoriel_" + jour + "_" + dateFileStr, targetFolder);
        log("Archive éditoriale créée et classée avec succès.");

        return jsonResponse({
          "status": "success",
          "message": "Éditorial [" + jour + "] publié et archivé",
          "logs": logs
        });

      } catch (edErr) {
        log("ERREUR publication éditorial : " + edErr.toString());
        return jsonResponse({
          "status": "error",
          "message": edErr.toString(),
          "logs": logs
        });
      }
    }

    // ==========================================
    // BRANCHE 6 : MISE À JOUR UNITAIRE D'UN SUJET (UPSERT)
    // ==========================================
    if (data.action === "update_single_subject") {
      log("Action reçue : MISE À JOUR UNITAIRE DU SUJET '" + data.sujet_title + "'");
      try {
        if (!MAIN_DOC_ID) throw new Error("Propriété MAIN_DOC_ID non configurée.");
        if (typeof data.sujet_title !== "string" || data.sujet_title.trim() === "") {
          throw new Error("Paramètre sujet_title invalide.");
        }
        if (typeof data.content !== "string") {
          throw new Error("Paramètre content invalide.");
        }

        var nowStr = new Date().toLocaleString("fr-FR", { timeZone: "Europe/Paris" });
        var resultStatus = upsertSubjectInDocument(MAIN_DOC_ID, data.sujet_title, data.content, nowStr);
        log("Résultat upsert : " + resultStatus);

        return jsonResponse({
          "status": "success",
          "message": resultStatus,
          "logs": logs
        });

      } catch (singleErr) {
        var detailErreur = singleErr.toString() + " | STACK: " + (singleErr.stack || "aucune stack");
        log("ERREUR mise à jour unitaire [v5] : " + detailErreur);
        return jsonResponse({
          "status": "error",
          "message": detailErreur,
          "logs": logs
        });
      }
    }

    // ==========================================
    // BRANCHE 3 : PUBLICATION DU RAPPORT DE VEILLE (PAR DÉFAUT)
    // ==========================================
    log("Action reçue : PUBLICATION DU RAPPORT DE VEILLE");
    if (!MAIN_DOC_ID || !ARCHIVE_FOLDER_ID) {
      throw new Error("Propriétés MAIN_DOC_ID ou ARCHIVE_FOLDER_ID non configurées.");
    }
    if (typeof data.content !== "string" || data.content.trim() === "") {
      throw new Error("Contenu du rapport vide ou invalide.");
    }

    var nowStr = new Date().toLocaleString("fr-FR", { timeZone: "Europe/Paris" });
    var dateFileStr = formatTimestampForFilename(new Date());

    // 1. Mise à jour sécurisée du doc principal (Google Sites)
    log("Mise à jour du document principal...");
    updateDocumentContent(MAIN_DOC_ID, data.content, nowStr);
    log("Document principal mis à jour avec succès.");

    // 2. Copie directe du document déjà formaté vers le dossier d'archive
    log("Création de l'archive 'Veille_" + dateFileStr + "' par copie directe...");
    var targetFolder = DriveApp.getFolderById(ARCHIVE_FOLDER_ID);
    var mainFile = DriveApp.getFileById(MAIN_DOC_ID);
    mainFile.makeCopy("Veille_" + dateFileStr, targetFolder);
    log("Archive créée et classée avec succès !");

    return jsonResponse({
      "status": "success",
      "logs": logs
    });

  } catch (error) {
    log("ERREUR GLOBALE : " + error.toString());
    return jsonResponse({
      "status": "error",
      "message": error.toString(),
      "logs": logs
    });
  }
}


/**
 * Formate un objet Date en AAAAMMJJ-HHMMSS pour le nommage des archives Drive.
 */
function formatTimestampForFilename(now) {
  var pad = function(n) { return n < 10 ? "0" + n : n; };
  return now.getFullYear().toString() +
         pad(now.getMonth() + 1) +
         pad(now.getDate()) + "-" +
         pad(now.getHours()) +
         pad(now.getMinutes()) +
         pad(now.getSeconds());
}


/**
 * 1. Déclenche l'autorisation de création, déplacement vers un dossier
 *    et mise à la corbeille sur Google Drive (scope Drive complet).
 */
function forceAuthMoveTo() {
  var archiveFolderId = PropertiesService.getScriptProperties().getProperty("ARCHIVE_FOLDER_ID");
  if (!archiveFolderId) {
    throw new Error("Définissez d'abord ARCHIVE_FOLDER_ID dans Paramètres du projet > Propriétés du script.");
  }
  var folder = DriveApp.getFolderById(archiveFolderId);

  var tempDoc = DocumentApp.create("temp_test_auth_move");
  tempDoc.saveAndClose();

  var file = DriveApp.getFileById(tempDoc.getId());
  file.moveTo(folder);
  file.setTrashed(true);

  Logger.log("Succès : autorisations Drive (moveTo / setTrashed) accordées.");
}


/**
 * 2. Déclenche l'autorisation de lecture et d'écriture dans un Google Doc
 *    (ouverture par ID, manipulation du Body et sauvegarde).
 */
function forceAuthWriteDoc() {
  var tempDoc = DocumentApp.create("temp_test_auth_write");
  var docId = tempDoc.getId();
  tempDoc.saveAndClose();

  var reopenedDoc = DocumentApp.openById(docId);
  var body = reopenedDoc.getBody();
  body.appendParagraph("Test d'écriture OAuth Apps Script.");
  reopenedDoc.saveAndClose();

  DriveApp.getFileById(docId).setTrashed(true);
  Logger.log("Succès : autorisations Google Docs (openById / write) accordées.");
}


/**
 * Remplace uniquement la section correspondant à `sujetTitle` (HEADING2) dans le document,
 * quelle que soit sa position (début, milieu ou fin de document),
 * ou l'ajoute à la fin si le sujet n'existe pas encore.
 */
function upsertSubjectInDocument(docId, sujetTitle, markdownContent, nowStr) {
  var doc = DocumentApp.openById(docId);
  var body = doc.getBody();
  var numChildren = body.getNumChildren();
  var targetHeadingIndex = -1;

  for (var i = 0; i < numChildren; i++) {
    var child = body.getChild(i);
    if (child.getType() === DocumentApp.ElementType.PARAGRAPH) {
      var para = child.asParagraph();
      if (para.getHeading() === DocumentApp.ParagraphHeading.HEADING2 &&
          para.getText().trim() === sujetTitle.trim()) {
        targetHeadingIndex = i;
        break;
      }
    }
  }

  var contentWithTimestamp = "_Dernière mise à jour ciblée : " + nowStr + "_\n\n" + markdownContent;

  if (targetHeadingIndex !== -1) {
    var countBeforeInsert = body.getNumChildren();

    // 1. Insérer D'ABORD le nouveau contenu juste après le titre H2
    populateMarkdownLinesAt(body, contentWithTimestamp, targetHeadingIndex + 1);

    var insertedCount = body.getNumChildren() - countBeforeInsert;
    var oldContentIndex = targetHeadingIndex + 1 + insertedCount;

    // 2. Supprimer ensuite les anciens éléments repoussés à partir de oldContentIndex
    while (oldContentIndex < body.getNumChildren()) {
      var oldChild = body.getChild(oldContentIndex);
      var elType = oldChild.getType();

      if (elType === DocumentApp.ElementType.HORIZONTAL_RULE) {
        break;
      }
      if (elType === DocumentApp.ElementType.PARAGRAPH) {
        var h = oldChild.asParagraph().getHeading();
        if (h === DocumentApp.ParagraphHeading.HEADING2 || h === DocumentApp.ParagraphHeading.HEADING1) {
          break;
        }
      }

      try {
        oldChild.removeFromParent();
      } catch (e) {
        if (elType === DocumentApp.ElementType.PARAGRAPH) {
          oldChild.asParagraph().setText(" ");
        }
        oldContentIndex++;
      }
    }

    doc.saveAndClose();
    return "[v7-fallback-clear] Sujet '" + sujetTitle + "' mis à jour sur place.";
  } else {
    if (body.getNumChildren() > 0 && body.getText().trim() !== "") {
      body.appendHorizontalRule();
    }
    var newHeading = body.appendParagraph(sujetTitle);
    newHeading.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    newHeading.setForegroundColor("#202124");

    populateMarkdownLinesAt(body, contentWithTimestamp, null);

    doc.saveAndClose();
    return "[v7-fallback-clear] Nouveau sujet '" + sujetTitle + "' ajouté en fin de document.";
  }
}


/**
 * Parcourt le contenu Markdown et construit la structure du Google Doc (avec entête globale).
 */
function populateDocBody(body, content, nowStr) {
  var headerPara = body.appendParagraph("🌐 Veille du " + nowStr);
  headerPara.setHeading(DocumentApp.ParagraphHeading.HEADING1);
  headerPara.setForegroundColor("#1a73e8");
  headerPara.setBold(true);

  populateMarkdownLinesAt(body, content, null);
}


/**
 * Parse et insère les lignes Markdown :
 * - si `insertIndex` est null : ajoute à la fin de `body` (append)
 * - si `insertIndex` est un entier : insère séquentiellement à partir de `insertIndex` sans copie/déplacement
 */
function populateMarkdownLinesAt(body, content, insertIndex) {
  var useInsert = (insertIndex !== null && insertIndex !== undefined);
  var cursor = useInsert ? insertIndex : 0;

  function addParagraph(text) {
    if (useInsert) {
      return body.insertParagraph(cursor++, text);
    }
    return body.appendParagraph(text);
  }

  function addListItem(text) {
    if (useInsert) {
      return body.insertListItem(cursor++, text);
    }
    return body.appendListItem(text);
  }

  function addHorizontalRule() {
    if (useInsert) {
      return body.insertHorizontalRule(cursor++);
    }
    return body.appendHorizontalRule();
  }

  function addTable(grid) {
    if (useInsert) {
      return body.insertTable(cursor++, grid);
    }
    return body.appendTable(grid);
  }

  var lines = content.split(/\r?\n/);
  var inRawBlock = false;
  var rawBlockLines = [];

  for (var i = 0; i < lines.length; i++) {
    var rawLine = lines[i];
    var line = rawLine.trim();

    if (line.startsWith('```')) {
      if (!inRawBlock) {
        inRawBlock = true;
        rawBlockLines = [];
      } else {
        inRawBlock = false;
        renderRawLetterBlockWithHelper(addTable, addParagraph, rawBlockLines);
        rawBlockLines = [];
      }
      continue;
    }

    if (inRawBlock) {
      rawBlockLines.push(rawLine);
      continue;
    }

    if (line === '') continue;

    if (line.startsWith('---')) {
      addHorizontalRule();
    }
    else if (line.startsWith('|') && line.indexOf('|', 1) !== -1 &&
             i + 1 < lines.length && isMarkdownTableSeparator(lines[i + 1].trim())) {
      var tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i].trim());
        i++;
      }
      i--;
      renderMarkdownTableWithHelper(addTable, addParagraph, tableLines);
    }
    else if (line.startsWith('# ')) {
      var p = addParagraph(line.replace(/^#\s+/, ''));
      p.setHeading(DocumentApp.ParagraphHeading.HEADING1);
      p.setForegroundColor("#1a73e8");
    }
    else if (line.startsWith('## ')) {
      var p = addParagraph(line.replace(/^##\s+/, ''));
      p.setHeading(DocumentApp.ParagraphHeading.HEADING2);
      p.setForegroundColor("#202124");
    }
    else if (line.startsWith('### ')) {
      var p = addParagraph(line.replace(/^###\s+/, ''));
      p.setHeading(DocumentApp.ParagraphHeading.HEADING3);
      p.setForegroundColor("#5f6368");
    }
    else if (line.match(/^#{4,6}\s+/)) {
      var cleanText = line.replace(/^#{4,6}\s+/, '');
      var p = addParagraph(cleanText);
      p.setHeading(DocumentApp.ParagraphHeading.HEADING4);
      p.setForegroundColor("#3c4043");
    }
    else if (line.match(/^[\-\*]\s+/)) {
      var cleanItem = line.replace(/^[\-\*]\s+/, '');
      var listItem = addListItem("");
      listItem.setGlyphType(DocumentApp.GlyphType.BULLET);
      parseAndAppendMarkdown(listItem, cleanItem);
    }
    else {
      var p = addParagraph("");
      parseAndAppendMarkdown(p, line);
    }
  }

  if (inRawBlock && rawBlockLines.length > 0) {
    renderRawLetterBlockWithHelper(addTable, addParagraph, rawBlockLines);
  }
}


function renderRawLetterBlockWithHelper(addTableFn, addParagraphFn, linesArray) {
  if (!linesArray || linesArray.length === 0) return;

  var fullText = linesArray.join("\n");
  var table = addTableFn([[fullText]]);
  table.setBorderColor("#dadce0");
  table.setBorderWidth(1);

  var cell = table.getCell(0, 0);
  cell.setBackgroundColor("#f8f9fa");
  cell.setPaddingTop(10);
  cell.setPaddingBottom(10);
  cell.setPaddingLeft(14);
  cell.setPaddingRight(14);

  var textObj = cell.editAsText();
  textObj.setFontFamily("Courier New");
  textObj.setFontSize(10);
  textObj.setForegroundColor("#202124");
  textObj.setBold(false);
  textObj.setItalic(false);

  addParagraphFn("");
}


function isMarkdownTableSeparator(line) {
  if (!line.startsWith('|')) return false;
  var inner = line.replace(/^\|/, '').replace(/\|$/, '');
  var cols = inner.split('|');
  if (cols.length === 0) return false;
  for (var c = 0; c < cols.length; c++) {
    if (!cols[c].trim().match(/^:?-{3,}:?$/)) {
      return false;
    }
  }
  return true;
}


function parseTableRow(line) {
  var cleaned = line.replace(/^\|/, '').replace(/\|$/, '');
  return cleaned.split('|').map(function(cell) {
    return cell.trim();
  });
}


function renderMarkdownTableWithHelper(addTableFn, addParagraphFn, tableLines) {
  if (!tableLines || tableLines.length < 2) return;

  var headerCells = parseTableRow(tableLines[0]);
  var separatorCells = parseTableRow(tableLines[1]);
  var numCols = headerCells.length;

  var alignments = [];
  for (var c = 0; c < numCols; c++) {
    var sep = (separatorCells[c] || "").trim();
    if (sep.startsWith(':') && sep.endsWith(':')) {
      alignments.push(DocumentApp.HorizontalAlignment.CENTER);
    } else if (sep.endsWith(':')) {
      alignments.push(DocumentApp.HorizontalAlignment.RIGHT);
    } else {
      alignments.push(DocumentApp.HorizontalAlignment.LEFT);
    }
  }

  var rowsData = [headerCells];
  for (var r = 2; r < tableLines.length; r++) {
    var rowCells = parseTableRow(tableLines[r]);
    while (rowCells.length < numCols) rowCells.push("");
    if (rowCells.length > numCols) rowCells = rowCells.slice(0, numCols);
    rowsData.push(rowCells);
  }

  var emptyGrid = [];
  for (var r = 0; r < rowsData.length; r++) {
    var emptyRow = [];
    for (var c = 0; c < numCols; c++) {
      emptyRow.push("");
    }
    emptyGrid.push(emptyRow);
  }

  var table = addTableFn(emptyGrid);
  table.setBorderColor("#dadce0");
  table.setBorderWidth(1);

  for (var r = 0; r < rowsData.length; r++) {
    var tableRow = table.getRow(r);
    var isHeader = (r === 0);

    for (var c = 0; c < numCols; c++) {
      var cell = tableRow.getCell(c);
      cell.setPaddingTop(6);
      cell.setPaddingBottom(6);
      cell.setPaddingLeft(8);
      cell.setPaddingRight(8);
      cell.setVerticalAlignment(DocumentApp.VerticalAlignment.TOP);

      if (isHeader) {
        cell.setBackgroundColor("#e8f0fe");
      } else if (r % 2 === 0) {
        cell.setBackgroundColor("#f8f9fa");
      } else {
        cell.setBackgroundColor("#ffffff");
      }

      var para = cell.getChild(0).asParagraph();
      para.setAlignment(alignments[c]);

      var cellText = rowsData[r][c];
      if (isHeader) {
        cellText = cellText.replace(/\*\*/g, '');
        var hText = para.appendText(cellText);
        resetTextFormatting(hText);
        hText.setBold(true);
        hText.setForegroundColor("#1a73e8");
      } else {
        parseAndAppendMarkdown(para, cellText);
      }
    }
  }

  addParagraphFn("");
}


function parseAndAppendMarkdown(containerElement, text) {
  var regex = /\[([^\]]+)\]\(([^)]+)\)|\*\*\*([^*]+)\*\*\*|\*\*([^*]+)\*\*|(?:\*|_)([^*_]+)(?:\*|_)/g;
  var lastIndex = 0;
  var match;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      var normalText = containerElement.appendText(text.substring(lastIndex, match.index));
      resetTextFormatting(normalText);
    }

    if (match[1] && match[2]) {
      var linkText = match[1].replace(/[\*_]/g, '');
      var linkUrl = match[2];
      var textElement = containerElement.appendText(linkText);
      resetTextFormatting(textElement);
      textElement.setLinkUrl(linkUrl);
      textElement.setForegroundColor("#1a73e8");
      textElement.setUnderline(true);
    }
    else if (match[3]) {
      var textElement = containerElement.appendText(match[3]);
      resetTextFormatting(textElement);
      textElement.setBold(true);
      textElement.setItalic(true);
    }
    else if (match[4]) {
      var textElement = containerElement.appendText(match[4]);
      resetTextFormatting(textElement);
      textElement.setBold(true);
    }
    else if (match[5]) {
      var textElement = containerElement.appendText(match[5]);
      resetTextFormatting(textElement);
      textElement.setItalic(true);
    }

    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    var trailingText = containerElement.appendText(text.substring(lastIndex));
    resetTextFormatting(trailingText);
  }
}


function resetTextFormatting(textElement) {
  textElement.setBold(false);
  textElement.setItalic(false);
  textElement.setUnderline(false);
  textElement.setForegroundColor("#202124");
  textElement.setLinkUrl(null);
}


function updateDocumentContent(docId, content, nowStr) {
  var doc = DocumentApp.openById(docId);
  var body = doc.getBody();

  var tempPara = body.appendParagraph("");

  while (body.getNumChildren() > 1) {
    body.getChild(0).removeFromParent();
  }

  populateDocBody(body, content, nowStr);

  if (tempPara.getParent() && tempPara.getText() === "") {
    tempPara.removeFromParent();
  }

  doc.saveAndClose();
}
