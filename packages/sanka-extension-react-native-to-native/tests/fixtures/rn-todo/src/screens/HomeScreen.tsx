import React, { useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import type { RootStackParamList } from "../../App";

type Props = NativeStackScreenProps<RootStackParamList, "Home">;
type Todo = { id: number; title: string; done: boolean };

export default function HomeScreen({ navigation }: Props) {
  const [title, setTitle] = useState("");
  const [todos, setTodos] = useState<Todo[]>([{ id: 1, title: "Buy milk", done: false }]);
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Todos ({todos.length})</Text>
      <TextInput style={styles.input} value={title} onChangeText={setTitle} placeholder="New todo" />
      <Pressable
        style={styles.button}
        onPress={() => {
          setTodos([...todos, { id: todos.length + 1, title, done: false }]);
          setTitle("");
        }}
      >
        <Text style={styles.buttonText}>Add</Text>
      </Pressable>
      <FlatList
        data={todos}
        keyExtractor={(item) => String(item.id)}
        renderItem={({ item }) => (
          <Pressable
            style={styles.row}
            onPress={() => navigation.navigate("Detail", { id: item.id, title: item.title })}
          >
            <Text style={styles.rowText}>{item.title}</Text>
            {item.done ? <Text>done</Text> : null}
          </Pressable>
        )}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16, backgroundColor: "#ffffff" },
  heading: { fontSize: 24, fontWeight: "700", marginBottom: 12 },
  input: { borderWidth: 1, borderColor: "#cccccc", borderRadius: 8, padding: 8 },
  button: { backgroundColor: "#0a84ff", padding: 12, borderRadius: 8, alignItems: "center", marginTop: 8 },
  buttonText: { color: "#ffffff", fontWeight: "600" },
  row: { paddingVertical: 12 },
  rowText: { fontSize: 16 },
});
