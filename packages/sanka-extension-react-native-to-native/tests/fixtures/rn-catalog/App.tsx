import React from "react";
import { NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import ItemsScreen from "./src/screens/ItemsScreen";
import ItemDetailScreen from "./src/screens/ItemDetailScreen";
import CreateItemScreen from "./src/screens/CreateItemScreen";
import SettingsScreen from "./src/screens/SettingsScreen";

export type RootStackParamList = {
  Items: undefined;
  Detail: { id: number };
  Create: undefined;
  Settings: undefined;
};

const Stack = createNativeStackNavigator<RootStackParamList>();

export default function App() {
  return (
    <NavigationContainer>
      <Stack.Navigator initialRouteName="Items">
        <Stack.Screen name="Items" component={ItemsScreen} options={{ title: "Catalog" }} />
        <Stack.Screen name="Detail" component={ItemDetailScreen} options={{ title: "Item" }} />
        <Stack.Screen name="Create" component={CreateItemScreen} options={{ title: "New item" }} />
        <Stack.Screen name="Settings" component={SettingsScreen} options={{ title: "Settings" }} />
      </Stack.Navigator>
    </NavigationContainer>
  );
}
