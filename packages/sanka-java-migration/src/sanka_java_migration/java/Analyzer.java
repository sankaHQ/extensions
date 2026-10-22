// SPDX-License-Identifier: Apache-2.0
// Parses syntax only: never loads application classes or invokes a build.
import com.github.javaparser.*;
import com.github.javaparser.ast.*;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.stmt.*;
import java.nio.file.*;
import java.util.*;

class Analyzer {
    static List<String> tokens(Node node) {
        var result = new ArrayList<String>();
        node.getTokenRange().ifPresent(range -> range.forEach(token -> {
            if (!token.getCategory().isWhitespaceOrComment()) result.add(token.getText());
        }));
        return result;
    }
    static List<String> modifiers(NodeList<Modifier> values) {
        return values.stream().map(m -> m.getKeyword().asString()).sorted().toList();
    }
    static Map<String,Object> map(Object... pairs) {
        var result = new LinkedHashMap<String,Object>();
        for (int i=0; i<pairs.length; i+=2) result.put((String)pairs[i], pairs[i+1]);
        return result;
    }
    static Object expr(Expression e) {
        if (e instanceof StringLiteralExpr s) return map("kind","string","value",s.asString());
        if (e instanceof IntegerLiteralExpr || e instanceof LongLiteralExpr)
            return map("kind","integer","value",e.toString());
        if (e instanceof BooleanLiteralExpr b) return map("kind","boolean","value",b.getValue());
        if (e instanceof NullLiteralExpr) return map("kind","null");
        if (e instanceof NameExpr n) return map("kind","name","value",n.getNameAsString());
        if (e instanceof BinaryExpr b && b.getOperator()==BinaryExpr.Operator.PLUS)
            return map("kind","plus","left",expr(b.getLeft()),"right",expr(b.getRight()));
        if (e instanceof MethodCallExpr m && m.getScope().isPresent() && m.getTypeArguments().isEmpty())
            return map("kind","call","scope",m.getScope().get().toString(),"name",m.getNameAsString(),
                "args",m.getArguments().stream().map(Analyzer::expr).toList());
        if (e instanceof ObjectCreationExpr c && c.getAnonymousClassBody().isEmpty()
                && c.getScope().isEmpty() && c.getTypeArguments().isEmpty())
            return map("kind","new","type",c.getType().asString(),
                "args",c.getArguments().stream().map(Analyzer::expr).toList());
        return map("kind","unsupported","syntax",e.getClass().getSimpleName());
    }
    static Object annotation(AnnotationExpr a) {
        var values = new LinkedHashMap<String,Object>();
        if (a instanceof SingleMemberAnnotationExpr s) values.put("value",expr(s.getMemberValue()));
        if (a instanceof NormalAnnotationExpr n) n.getPairs().forEach(p -> values.put(p.getNameAsString(),expr(p.getValue())));
        return map("name",a.getNameAsString(),"values",values,"tokens",tokens(a));
    }
    static Object parameter(Parameter p) {
        return map("name",p.getNameAsString(),"type",p.getType().asString(),
            "annotations",p.getAnnotations().stream().map(Analyzer::annotation).toList(),
            "plain",p.getModifiers().isEmpty() && !p.isVarArgs() && p.getType().getAnnotations().isEmpty());
    }
    static Object method(MethodDeclaration m) {
        var statements = m.getBody().map(BlockStmt::getStatements).orElse(new NodeList<>());
        Object result = null;
        if (statements.size()==1 && statements.get(0) instanceof ReturnStmt r && r.getExpression().isPresent())
            result=expr(r.getExpression().get());
        return map("name",m.getNameAsString(),"type",m.getType().asString(),"line",m.getBegin().get().line,
            "annotations",m.getAnnotations().stream().map(Analyzer::annotation).toList(),
            "parameters",m.getParameters().stream().map(Analyzer::parameter).toList(),
            "plain",m.getModifiers().size()==1 && m.isPublic() && m.getTypeParameters().isEmpty()
                && m.getThrownExceptions().isEmpty() && m.getType().getAnnotations().isEmpty(),
            "body",m.getBody().map(Node::toString).orElse(""),"expression",result,
            "body_tokens",m.getBody().map(Analyzer::tokens).orElse(List.of()),
            "modifiers",modifiers(m.getModifiers()),
            "throws",m.getThrownExceptions().size(),"type_parameters",m.getTypeParameters().size(),
            "type_annotations",m.getType().getAnnotations().size(),"receiver",m.getReceiverParameter().isPresent(),
            "main",m.isPublic() && m.isStatic() && m.getModifiers().size()==2
                && m.getNameAsString().equals("main") && m.getType().asString().equals("void")
                && m.getParameters().size()==1 && m.getParameter(0).getType().asString().equals("String[]")
                && m.getAnnotations().isEmpty() && m.getThrownExceptions().isEmpty()
                && m.getTypeParameters().isEmpty());
    }
    static Object declaration(TypeDeclaration<?> t) {
        boolean shape=t.getModifiers().size()==1 && t.isPublic();
        if (t instanceof ClassOrInterfaceDeclaration c)
            shape &= !c.isInterface() && c.getExtendedTypes().isEmpty() && c.getImplementedTypes().isEmpty()
                && c.getTypeParameters().isEmpty();
        else if (t instanceof RecordDeclaration r)
            shape &= r.getImplementedTypes().isEmpty() && r.getTypeParameters().isEmpty();
        else shape=false;
        return map("name",t.getNameAsString(),"kind",t.getClass().getSimpleName(),"plain",shape,
            "modifiers",modifiers(t.getModifiers()),
            "interface",t instanceof ClassOrInterfaceDeclaration c && c.isInterface(),
            "extends",t instanceof ClassOrInterfaceDeclaration c ? c.getExtendedTypes().stream().map(Node::toString).toList() : List.of(),
            "implements",t instanceof ClassOrInterfaceDeclaration c ? c.getImplementedTypes().stream().map(Node::toString).toList() : List.of(),
            "type_parameters",t instanceof ClassOrInterfaceDeclaration c ? c.getTypeParameters().size() : 0,
            "line",t.getBegin().get().line,
            "annotations",t.getAnnotations().stream().map(Analyzer::annotation).toList(),
            "components",t instanceof RecordDeclaration r ? r.getParameters().stream().map(Analyzer::parameter).toList() : List.of(),
            "methods",t.getMethods().stream().map(Analyzer::method).toList(),
            "fields",t.getFields().stream().map(f -> map("modifiers",modifiers(f.getModifiers()),
                "annotations",f.getAnnotations().stream().map(Analyzer::annotation).toList(),
                "variables",f.getVariables().stream().map(v -> map("name",v.getNameAsString(),"type",v.getType().asString(),
                    "type_annotations",v.getType().getAnnotations().size(),
                    "initializer",v.getInitializer().map(Analyzer::tokens).orElse(List.of()))).toList())).toList(),
            "constructors",t.getConstructors().stream().map(c -> map("modifiers",modifiers(c.getModifiers()),
                "annotations",c.getAnnotations().size(),"throws",c.getThrownExceptions().size(),
                "parameters",c.getParameters().stream().map(Analyzer::parameter).toList(),
                "type_parameters",c.getTypeParameters().size(),"body_tokens",tokens(c.getBody()))).toList(),
            "other_members",t.getMembers().stream().filter(m -> !(m instanceof MethodDeclaration)).map(m -> m.getClass().getSimpleName()).toList());
    }
    static String json(Object value) {
        if (value==null) return "null";
        if (value instanceof String s) {
            var b=new StringBuilder("\"");
            for(char c:s.toCharArray()) {
                if(c=='"' || c=='\\') b.append('\\').append(c);
                else if(c<32 || c>=127) b.append(String.format("\\u%04x",(int)c));
                else b.append(c);
            }
            return b.append('"').toString();
        }
        if(value instanceof Map<?,?> m) return "{"+String.join(",",m.entrySet().stream().map(e -> json(e.getKey())+":"+json(e.getValue())).toList())+"}";
        if(value instanceof List<?> l) return "["+String.join(",",l.stream().map(Analyzer::json).toList())+"]";
        return value.toString();
    }
    public static void main(String[] args) throws Exception {
        var parser=new JavaParser(new ParserConfiguration().setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21));
        var results=new ArrayList<Object>();
        for(String file:args) {
            var parsed=parser.parse(Path.of(file));
            if(!parsed.isSuccessful()) throw new IllegalArgumentException("Cannot parse "+file+": "+parsed.getProblems());
            var cu=parsed.getResult().orElseThrow();
            results.add(map("file",file,"package",cu.getPackageDeclaration().map(p -> p.getNameAsString()).orElse(""),
                "imports",cu.getImports().stream().map(i -> map("name",i.getNameAsString(),"static",i.isStatic(),"wildcard",i.isAsterisk())).toList(),
                "plain",cu.getModule().isEmpty() && cu.getPackageDeclaration().map(p -> p.getAnnotations().isEmpty()).orElse(false),
                "types",cu.getTypes().stream().map(Analyzer::declaration).toList()));
        }
        System.out.println(json(results));
    }
}
